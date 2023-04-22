from types import ModuleType

from lightning.app.utilities.imports import _is_gradio_available

if _is_gradio_available():
    import gradio
else:
    gradio = ModuleType("gradio")
    gradio.themes = ModuleType("gradio.themes")

    class DummyColor:
        def __init__(self, *args, **kwargs):
            pass

    class DummyDefault:
        def __init__(self, *args, **kwargs):
            pass

    gradio.themes.Default = DummyDefault
    gradio.themes.Color = DummyColor

theme = gradio.themes.Default(
    primary_hue=gradio.themes.Color(
        "#ffffff",
        "#e9d5ff",
        "#d8b4fe",
        "#c084fc",
        "#fcfcfc",
        "#a855f7",
        "#9333ea",
        "#8823e1",
        "#6b21a8",
        "#2c2730",
        "#1c1c1c",
    ),
    secondary_hue=gradio.themes.Color(
        "#c3a1e8",
        "#e9d5ff",
        "#d3bbec",
        "#c795f9",
        "#9174af",
        "#a855f7",
        "#9333ea",
        "#6700c2",
        "#000000",
        "#991ef1",
        "#33243d",
    ),
    neutral_hue=gradio.themes.Color(
        "#ede9fe",
        "#ddd6fe",
        "#c4b5fd",
        "#a78bfa",
        "#fafafa",
        "#8b5cf6",
        "#7c3aed",
        "#6d28d9",
        "#6130b0",
        "#8a4ce6",
        "#3b3348",
    ),
).set(
    body_background_fill="*primary_50",
    body_background_fill_dark="*primary_950",
    body_text_color_dark="*primary_100",
    body_text_size="*text_sm",
    body_text_color_subdued_dark="*primary_100",
    background_fill_primary="*primary_50",
    background_fill_primary_dark="*primary_950",
    background_fill_secondary="*primary_50",
    background_fill_secondary_dark="*primary_950",
    border_color_accent="*primary_400",
    border_color_accent_dark="*primary_900",
    border_color_primary="*primary_600",
    border_color_primary_dark="*primary_800",
    color_accent="*primary_400",
    color_accent_soft="*primary_300",
    color_accent_soft_dark="*primary_700",
    link_text_color="*primary_500",
    link_text_color_dark="*primary_50",
    link_text_color_active="*secondary_800",
    link_text_color_active_dark="*primary_500",
    link_text_color_hover="*primary_400",
    link_text_color_hover_dark="*primary_400",
    link_text_color_visited="*primary_500",
    link_text_color_visited_dark="*secondary_100",
    block_background_fill="*primary_50",
    block_background_fill_dark="*primary_900",
    block_border_color_dark="*primary_800",
    checkbox_background_color="*primary_50",
    checkbox_background_color_dark="*primary_50",
    checkbox_background_color_focus="*primary_100",
    checkbox_background_color_focus_dark="*primary_100",
    checkbox_background_color_hover="*primary_400",
    checkbox_background_color_hover_dark="*primary_500",
    checkbox_background_color_selected="*primary_300",
    checkbox_background_color_selected_dark="*primary_500",
    checkbox_border_color_dark="*primary_200",
    checkbox_border_radius="*radius_md",
    input_background_fill="*primary_50",
    input_background_fill_dark="*primary_900",
    input_radius="*radius_xxl",
    slider_color="*primary_600",
    slider_color_dark="*primary_700",
    button_large_radius="*radius_xxl",
    button_large_text_size="*text_md",
    button_small_radius="*radius_xxl",
    button_primary_background_fill_dark="*primary_800",
    button_primary_background_fill_hover_dark="*primary_700",
    button_primary_border_color_dark="*primary_800",
    button_secondary_background_fill="*neutral_200",
    button_secondary_background_fill_dark="*primary_600",
)
