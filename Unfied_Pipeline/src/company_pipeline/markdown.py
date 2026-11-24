"""Markdown rendering for extracted PDF pages."""


def pages_to_markdown(title: str, pages: list[str]) -> str:
    sections = [f"# {title.strip() or 'Processed document'}"]
    for page_number, text in enumerate(pages, start=1):
        sections.extend([f"## Page {page_number}", text.strip() or "_No text extracted._"])
    return "\n\n".join(sections).rstrip() + "\n"
