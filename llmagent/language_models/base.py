from pydantic import BaseSettings, BaseModel
from abc import ABC, abstractmethod
from enum import Enum
from typing import List, Tuple, Union
from llmagent.mytypes import Document
from llmagent.cachedb.redis_cachedb import RedisCacheConfig
from llmagent.utils.configuration import settings
from llmagent.utils.output.printing import show_if_debug
from llmagent.prompts.templates import (
    EXTRACTION_PROMPT_GPT4,
    SUMMARY_ANSWER_PROMPT_GPT4,
)
from llmagent.prompts.dialog import collate_chat_history
import aiohttp
import asyncio


class LLMConfig(BaseSettings):
    type: str = "openai"
    max_tokens: int = 1024
    # chat_model: str = "gpt-3.5-turbo"
    # completion_model: str = "text-davinci-003"
    use_chat_for_completion: bool = True # use chat model for completion?
    stream: bool = False  # stream output from API?
    cache_config: RedisCacheConfig = RedisCacheConfig(
        hostname="redis-11524.c251.east-us-mz.azure.cloud.redislabs.com",
        port=11524,
    )


class LLMResponse(BaseModel):
    message: str
    usage: int
    cached: bool = False


class Role(str, Enum):
    USER = "user"
    SYSTEM = "system"
    ASSISTANT = "assistant"


class LLMMessage(BaseModel):
    role: Role
    name: str = "xyz"
    content: str

    def __str__(self):
        return f"{self.role} ({self.name}): {self.content}"


# Define an abstract base class for language models
class LanguageModel(ABC):
    """
    Abstract base class for language models.
    """

    max_tokens: int = None

    @staticmethod
    def create(config: LLMConfig):
        """
        Create a language model.
        Args:
            config: configuration for language model
        Returns: instance of language model
        """
        from llmagent.language_models.openai_gpt import OpenAIGPT

        cls = dict(
            openai=OpenAIGPT,
        ).get(config.type, OpenAIGPT)
        return cls(config)

    @abstractmethod
    def set_stream(self, stream: bool) -> bool:
        """Enable or disable streaming output from API.
        Return previous value of stream."""
        pass

    @abstractmethod
    def get_stream(self) -> bool:
        """Get streaming status"""
        pass

    @abstractmethod
    def generate(self, prompt: str, max_tokens: int) -> LLMResponse:
        pass

    @abstractmethod
    def chat(self,
             messages: Union[str, List[LLMMessage]],
             max_tokens: int
             ) -> LLMResponse:
        pass

    def __call__(self, prompt: str, max_tokens: int) -> LLMResponse:
        return self.generate(prompt, max_tokens)

    def followup_to_standalone(
        self, chat_history: List[Tuple[str]], question: str
    ) -> str:
        """
        Given a chat history and a question, convert it to a standalone question.
        Args:
            chat_history: list of tuples of (question, answer)
            query: follow-up question

        Returns: standalone version of the question
        """
        history = collate_chat_history(chat_history)

        prompt = f"""
        Given the conversationn below, and a follow-up question, rephrase the follow-up 
        question as a standalone question.
        
        Chat history: {history}
        Follow-up question: {question} 
        """.strip()
        show_if_debug(prompt, "FOLLOWUP->STANDALONE-PROMPT= ")
        standalone = self.generate(prompt=prompt, max_tokens=1024).message.strip()
        show_if_debug(prompt, "FOLLOWUP->STANDALONE-RESPONSE= ")
        return standalone

    async def get_verbatim_extract_async(self, question: str, passage: Document) -> str:
        """
        Asynchronously, get verbatim extract from passage
        that is relevant to a question.
        Asynch allows parallel calls to the LLM API.
        """
        async with aiohttp.ClientSession():
            templatized_prompt = EXTRACTION_PROMPT_GPT4
            final_prompt = templatized_prompt.format(
                question=question, content=passage.content
            )
            show_if_debug(final_prompt, "EXTRACT-PROMPT= ")
            final_extract = await self.agenerate(prompt=final_prompt, max_tokens=1024)
            show_if_debug(final_extract.message.strip(), "EXTRACT-RESPONSE= ")
        return final_extract.message.strip()

    async def _get_verbatim_extracts(
        self,
        question: str,
        passages: List[Document],
    ) -> List[str]:
        async with aiohttp.ClientSession():
            verbatim_extracts = await asyncio.gather(
                *(self.get_verbatim_extract_async(question, P) for P in passages)
            )
        metadatas = [P.metadata for P in passages]
        # return with metadata so we can use it downstream, e.g. to cite sources
        return [
            Document(content=e, metadata=m)
            for e, m in zip(verbatim_extracts, metadatas)
        ]

    def get_verbatim_extracts(
        self, question: str, passages: List[Document]
    ) -> List[Document]:
        """
        From each passage, extract verbatim text that is relevant to a question,
        using concurrent API calls to the LLM.
        Args:
            question: question to be answered
            passages: list of passages from which to extract relevant verbatim text
            LLM: LanguageModel to use for generating the prompt and extract
        Returns:
            list of verbatim extracts from passages that are relevant to question
        """
        docs = asyncio.run(self._get_verbatim_extracts(question, passages))
        return docs

    def get_summary_answer(self, question: str, passages: List[Document]) -> Document:
        """
        Given a question and a list of (possibly) doc snippets,
        generate an answer if possible
        Args:
            question: question to answer
            passages: list of `Document` objects each containing a possibly relevant
                snippet, and metadata
        Returns:
            a `Document` object containing the answer,
            and metadata containing source citations

        """

        # Define an auxiliary function to transform the list of passages into a single string
        def stringify_passages(passages):
            return "\n".join(
                [
                    f"""
                Extract: {p.content}
                Source: {p.metadata["source"]}
                """
                    for p in passages
                ]
            )

        passages = stringify_passages(passages)
        # Substitute Q and P into the templatized prompt

        final_prompt = SUMMARY_ANSWER_PROMPT_GPT4.format(
            question=f"Question:{question}", extracts=passages
        )
        show_if_debug(final_prompt, "SUMMARIZE_PROMPT= ")
        # Generate the final verbatim extract based on the final prompt
        llm_response = self.generate(
            prompt=final_prompt, max_tokens=1024
        )
        final_answer = llm_response.message.strip()
        show_if_debug(final_answer, "SUMMARIZE_RESPONSE= ")
        parts = final_answer.split("SOURCE:", maxsplit=1)
        if len(parts) > 1:
            content = parts[0].strip()
            sources = parts[1].strip()
        else:
            content = final_answer
            sources = ""
        return Document(
            content=content,
            metadata={"source": "SOURCE: " + sources,
                      "cached": llm_response.cached}
        )


class StreamingIfAllowed:
    """Context to temporarily enable streaming, if allowed globally via
    `settings.stream`"""

    def __init__(self, llm: LanguageModel, stream: bool = True):
        self.llm = llm
        self.stream = stream

    def __enter__(self):
        self.old_stream = self.llm.set_stream(settings.stream and self.stream)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.llm.set_stream(self.old_stream)
