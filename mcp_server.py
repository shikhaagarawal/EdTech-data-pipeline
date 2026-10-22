import sys
from mcp.server.mcpserver import MCPServer
import retrieval
import logging
import time
from typing import Annotated
from pydantic import Field

logging.basicConfig(level = logging.INFO)
logger = logging.getLogger(__name__)

mcp = MCPServer("prealgebra-textbook")

SESSION = "default"  # single-user local server; not exposed to the model
SEARCH_HINT = (
    "For more content on this topic, call next_chunk (no arguments). "
    "next_chunk can be called multiple times to get all relevant content. Max 5 times in a row."
    "Do not call search_documents again unless the user changes topic or ask for practice questions"
)
END_HINT = "No more content for this topic. Do not search again."

def _render(chunk: dict, hint: str) -> str:
    return (
        f"TEXTBOOK PASSAGE (page {chunk.get('page')}) is between the markers below.\n\n"
        "<<<BEGIN PASSAGE>>>\n"
        f"{chunk.get('text')}\n"
        "<<<END PASSAGE>>>\n\n"
        "Instructions: output the passage as normal rendered markdown. Keep "
        "every heading, bold, italic, list, and table exactly as written. "
        "Do NOT put it in a code block or blockquote, and do not remove or "
        "convert the markdown symbols. Do not reword anything. Start with the "
        f"line '**Page {chunk.get('page')}**', then a horizontal rule (---), "
        "then the passage. After the passage, add at most two sentences of "
        f"your own, separated by another ---. {hint}"
        "Your added sentences (at most two) must only restate or clarify what is "
        "written in the passage above. Do not add facts, formulas, examples, or "
        "definitions that are not in the passage. If the passage doesn't cover "
        "something, say so instead of filling it in. "
    )

@mcp.tool()
def search_documents(
query: Annotated[str, Field(max_length=40, description=(
        "The user's topic in their own words, 1-4 words, e.g. 'circles', "
        "'fractions', 'ratios'. Copy it from the user's message. Do not "
        "add keywords, synonyms, or sub-topics, and do not rephrase."
    ))]
) -> str:
    """Open the Prealgebra textbook at the passage that best matches a topic.

    Works like a book reader: the user wants to read the passage as written.
    Relay it as rendered markdown with its words unchanged, keeping all
    headings, bold, italic, lists, and tables. Never put it in a code block
    or blockquote, and do not summarize or reword it. Start with the page
    number. You may add at most two sentences after the passage, separated
    by a horizontal rule.

    Call this ONCE per topic. Call next_chunk 5 times and then stop.

    Args:
        query: The topic as a short phrase, e.g. "area of a circle".
    """
    chunk = retrieval.ask(SESSION, query, debug=False)
    return _render(chunk, SEARCH_HINT)


@mcp.tool()
def next_chunk() -> str:
    """Turn to the next passage of the textbook topic being read.

    Takes no arguments. Relay the passage as rendered markdown with its
    words unchanged, keeping all headings, bold, italic, lists, and tables.
    Never use a code block or blockquote, and do not summarize or reword.
    Start with the page number, then stop, with at most two sentences of
    your own after it.
    """
    result = retrieval.next_chunk(SESSION)
    # Adjust this check to however your next_chunk signals "nothing left"
    if not result or not result.get("text"):
        return END_HINT
    return _render(result, SEARCH_HINT)


@mcp.tool()
def recap() -> dict:
    """Summarize what has been covered so far in this session. Takes no arguments."""
    return retrieval.recap(SESSION)

@mcp.tool()
def reset_session() -> str:
    """Clear the reading position and history so a new topic starts fresh.
    Takes no arguments. Only call this when the user asks to reset or start over."""
    retrieval.reset(SESSION)
    return "Session cleared. Use search_documents to start a new topic."


if __name__ == "__main__":
    started = time.monotonic()
    logger.info("Loading retrieval models and Qdrant client")

    retrieval._init()

    logger.info("Retrieval ready after %.2fs", time.monotonic() - started)
    mcp.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=8000,
        json_response=True,
        stateless_http=True,
    )

