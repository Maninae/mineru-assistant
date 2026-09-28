"""LLM-powered semantic tag suggestion via Ollama."""

import json
import re
import urllib.error
import urllib.request


def suggest_tags(
    query: str,
    all_tags: list[str],
    model: str,
    top_n: int,
    base_url: str,
) -> list[str]:
    """Use an LLM to suggest the most relevant tags for a query.

    Args:
        query: Natural language search query.
        all_tags: All available tags in the index.
        model: Ollama model name.
        top_n: Maximum number of tags to return.
        base_url: Ollama API base URL.

    Returns:
        List of suggested tag strings that exist in the index.

    Raises:
        ConnectionError: If Ollama is unavailable.
    """
    tag_list = "\n".join(f"- {t}" for t in all_tags)
    prompt = (
        f"Given these tags:\n{tag_list}\n\n"
        f"Which {top_n} tags are most relevant to this query: \"{query}\"\n\n"
        f"Reply with ONLY the tag names, one per line, no numbering or explanation."
    )

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{base_url}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError) as e:
        raise ConnectionError(f"Ollama unavailable at {base_url}: {e}") from e

    response_text = data.get("response", "")
    return _parse_tags(response_text, all_tags, top_n)


def _parse_tags(response_text: str, all_tags: list[str], top_n: int) -> list[str]:
    """Extract valid tag names from LLM response text.

    Args:
        response_text: Raw text from the LLM.
        all_tags: Valid tags to match against.
        top_n: Maximum tags to return.

    Returns:
        List of matched tags.
    """
    tags_lower = {t.lower(): t for t in all_tags}
    matched: list[str] = []

    for line in response_text.strip().split('\n'):
        # Strip numbering, bullets, dashes, asterisks
        cleaned = re.sub(r'^[\d\.\)\-\*\s]+', '', line).strip().strip('`"\'')
        cleaned_lower = cleaned.lower()
        if cleaned_lower in tags_lower and tags_lower[cleaned_lower] not in matched:
            matched.append(tags_lower[cleaned_lower])
        if len(matched) >= top_n:
            break

    return matched
