"""Literal text files keep user-facing text out of FFmpeg filter syntax."""


def draw_text(
    file: str,
    size: int,
    x: str,
    y: str,
    *,
    color: str = "0xeaf3ff",
    enable: str | None = None,
    font: str = "font.ttf",
    line_spacing: int = 0,
) -> str:
    # Font and text paths are generated ASCII names, never user-provided filter syntax.
    result = (
        f"drawtext=fontfile={font}:textfile=text/{file}.txt:expansion=none:"
        f"fontsize={size}:fontcolor={color}:x='{x}':y='{y}':line_spacing={line_spacing}"
    )
    if enable is not None:
        result += f":enable='{enable}'"
    return result
