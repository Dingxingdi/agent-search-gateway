"""Pure prompt builders for gateway LLM stages."""

from ..input_limits import TruncatableMessage
from ..providers.contracts import ChatMessage


def judge_messages(candidate: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Decide whether the supplied text is usable main-body content for the target "
                "web page. Return JSON with boolean field ok and optional short string field "
                "reason."
            ),
        },
        TruncatableMessage("Candidate content:\n", candidate),
    )


def safety_messages(content: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Decide whether the supplied final page content is safe to return. "
                "Return JSON with boolean field ok and optional short string field reason."
            ),
        },
        TruncatableMessage("Final content:\n", content),
    )


def content_clean_messages(raw_content: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Return only the cleaned, readable main content from the supplied raw page text."
            ),
        },
        TruncatableMessage("", raw_content),
    )


def focus_summary_messages(content: str, focus: str) -> tuple[ChatMessage, ...]:
    normalized_focus = focus.strip()
    return (
        {
            "role": "system",
            "content": "Summarize only the supplied page content, emphasizing the requested focus.",
        },
        TruncatableMessage(f"Focus: {normalized_focus}\n\nPage content:\n", content),
    )


def llm_search_messages(prompt: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Find relevant web results. Return only repeated blocks in this exact "
                "restricted format:\n## Result\nURL: https://example.com/page\n"
                "Abstract: concise non-empty summary\n"
                "Do not use Markdown links or other result formats."
            ),
        },
        TruncatableMessage("", prompt.strip()),
    )


def llm_paper_search_messages(prompt: str) -> tuple[ChatMessage, ...]:
    return (
        {
            "role": "system",
            "content": (
                "Find relevant academic papers. Return only repeated blocks in exactly this "
                "16-line restricted format, with every field present exactly once and in this "
                "order. "
                "Optional values may be empty. Authors and Topics are semicolon-separated. "
                "Dates are empty or YYYY-MM-DD. Citations are empty or a non-negative integer. "
                "Open Access is true, false, or unknown. Do not use Markdown links, Result blocks, "
                "or any text outside these blocks:\n"
                "## Paper\n"
                "Title: paper title\n"
                "Authors: Alice Author; Bob Author\n"
                "Abstract: concise abstract\n"
                "DOI: 10.1000/example\n"
                "arXiv: 2401.12345\n"
                "Published: 2024-01-02\n"
                "Updated: 2024-02-03\n"
                "URL: https://example.com/paper\n"
                "PDF: https://example.com/paper.pdf\n"
                "Venue: venue name\n"
                "Topics: topic one; topic two\n"
                "Citations: 0\n"
                "Open Access: unknown\n"
                "OA Status: \n"
                "License: "
            ),
        },
        TruncatableMessage("", prompt.strip()),
    )
