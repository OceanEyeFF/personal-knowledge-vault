"""
Personal Knowledge Vault - 主题颜色定义
用于 QTextBrowser 中的 HTML 内联样式，以及 Python 代码中的动态颜色引用。
"""

# 语义化颜色字典
THEME_COLORS = {
    "light": {
        "user_bg": "#E3F2FD",
        "user_border": "#2196F3",
        "assistant_bg": "#F5F5F5",
        "assistant_border": "#4CAF50",
        "role_label": "#757575",
        "code_bg": "#272822",
        "code_fg": "#f8f8f2",
        "msg_fg": "#2c2c2c",
        "ref_card_bg": "#E8F5E9",
        "ref_card_border": "#4CAF50",
        "ref_card_meta": "#666666",
        "ref_card_summary": "#555555",
        "status_info": "#1976D2",
        "status_success": "#4CAF50",
        "status_error": "#F44336",
        "status_warning": "#FF9800",
        "status_progress": "#666666",
        "warning_bg": "#FFF3E0",
        "warning_fg": "#E65100",
        "display_bg": "#FFFFFF",
    },
    "dark": {
        "user_bg": "#1A3A5C",
        "user_border": "#42A5F5",
        "assistant_bg": "#2D2D30",
        "assistant_border": "#66BB6A",
        "role_label": "#9E9E9E",
        "code_bg": "#1E1E1E",
        "code_fg": "#D4D4D4",
        "msg_fg": "#D4D4D4",
        "ref_card_bg": "#1B3D1B",
        "ref_card_border": "#66BB6A",
        "ref_card_meta": "#9E9E9E",
        "ref_card_summary": "#B0B0B0",
        "status_info": "#42A5F5",
        "status_success": "#66BB6A",
        "status_error": "#EF5350",
        "status_warning": "#FFA726",
        "status_progress": "#9E9E9E",
        "warning_bg": "#3E2723",
        "warning_fg": "#FFB74D",
        "display_bg": "#1E1E1E",
    }
}

_current_theme = "light"

def set_current_theme(theme: str):
    """设置当前全局主题 ('light' 或 'dark')"""
    global _current_theme
    if theme in THEME_COLORS:
        _current_theme = theme

def get_current_colors() -> dict:
    """获取当前主题的颜色字典"""
    return THEME_COLORS.get(_current_theme, THEME_COLORS["light"])
