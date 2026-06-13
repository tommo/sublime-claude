"""
Generate a .hidden-color-scheme enumerating fg x bg scope rules so the terminal
renderer can color cells via add_regions(scope="<prefix>.<fg>.<bg>").

Ported from Terminus (tools/theme_generator.py). The "trick": Sublime only colors
regions via color-scheme scopes, so we statically pre-generate every (fg, bg)
combination across the 16 ANSI colors + default/reverse + the full 256 palette.
24-bit truecolor is approximated to the nearest 256 color by the renderer.
"""
import os
import json
from copy import deepcopy
from collections import OrderedDict

import pyte

SCOPE_PREFIX = "claude_terminal_color"
DEFAULT_BACKGROUND = "#262626"

ANSI_COLORS = [
    "black", "red", "green", "brown", "blue", "magenta", "cyan", "white",
    "light_black", "light_red", "light_green", "light_brown",
    "light_blue", "light_magenta", "light_cyan", "light_white",
]

# Palette matching ClaudeCode.hidden-color-scheme so named colors look identical.
DEFAULT_VARIABLES = OrderedDict([
    ("background", DEFAULT_BACKGROUND),
    ("foreground", "#ffffff"),
    ("caret", "white"),
    ("block_caret", "white"),
    ("selection", "#444444"),
    ("selection_foreground", "#ffffff"),
    ("black", "#000000"),
    ("red", "#cd0000"),
    ("green", "#00cd00"),
    ("brown", "#cdcd00"),
    ("blue", "#0000ee"),
    ("magenta", "#cd00cd"),
    ("cyan", "#00cdcd"),
    ("white", "#e5e5e5"),
    ("light_black", "#7f7f7f"),
    ("light_red", "#ff0000"),
    ("light_green", "#00ff00"),
    ("light_brown", "#ffff00"),
    ("light_blue", "#5c5cff"),
    ("light_magenta", "#ff00ff"),
    ("light_cyan", "#00ffff"),
    ("light_white", "#ffffff"),
])

DEFAULT_GLOBALS = OrderedDict([
    ("background", "var(background)"),
    ("foreground", "var(foreground)"),
    ("caret", "var(caret)"),
    ("block_caret", "var(block_caret)"),
    ("selection", "var(selection)"),
    ("selection_foreground", "var(selection_foreground)"),
    ("selection_corner_style", "square"),
    ("selection_border_width", "0"),
])


def next_color(color_text):
    """Given "#xxxxxy", return the next color "#xxxx{xy+1}" (or -1 at ff)."""
    hex_value = int(color_text[5:], 16)
    if hex_value == 255:
        return color_text[:5] + "fe"
    return color_text[:5] + "{:2x}".format(hex_value + 1).replace(" ", "0")


def generate_theme_file(path, variables=None, globals=None, scope_prefix=SCOPE_PREFIX,
                        ansi_scopes=True, color256_scopes=True, background=None,
                        pretty=False, foreground_only=True):
    variables = OrderedDict(variables or DEFAULT_VARIABLES)
    globals = OrderedDict(globals or DEFAULT_GLOBALS)

    scheme = OrderedDict(name="ClaudeTerminal", variables=OrderedDict(), globals=OrderedDict())

    _colors16 = OrderedDict()
    for i in range(16):
        _colors16[ANSI_COLORS[i]] = "#{}".format(pyte.graphics.FG_BG_256[i])

    if "caret" not in variables and "foreground" in variables:
        variables["caret"] = variables["foreground"]
    scheme["variables"].update(variables)
    scheme["variables"].update(_colors16)
    scheme["variables"].update(variables)
    scheme["globals"].update(globals)

    # add_regions inverts fg/bg when a scope's background equals the theme
    # background (SublimeTextIssues/Core#817). Nudge the theme background by one.
    if not background and "background" in scheme["variables"]:
        background = scheme["variables"]["background"]
        scheme["variables"]["background"] = next_color(background)
        scheme["globals"]["background"] = background

    for key, value in scheme["variables"].items():
        if key == "background":
            continue
        if value == background:
            scheme["variables"][key] = next_color(value)

    colors = OrderedDict()
    if ansi_scopes:
        colors.update(_colors16)
        colors["default"] = "#default"
        colors["reverse_default"] = "#reverse_default"
    if color256_scopes:
        for rgb in pyte.graphics.FG_BG_256:
            colors[rgb] = "#{}".format(rgb)

    def resolve_fg(u, ucolor):
        if u in ANSI_COLORS:
            return "var({})".format(u)
        if ucolor == "#default":
            return "var(foreground)"
        if ucolor == "#reverse_default":
            return "var(background)"
        return ucolor

    def resolve_bg(v, vcolor):
        if v in ANSI_COLORS:
            return "var({})".format(v)
        if vcolor == "#default":
            return "var(background)"
        if vcolor == "#reverse_default":
            return "var(foreground)"
        if vcolor == background:
            return next_color(vcolor)
        return vcolor

    scheme["rules"] = []
    if foreground_only:
        # One rule per foreground color: scope "<prefix>.<fg>".
        # add_regions(default flags) colors text with scope fg AND fills with
        # scope bg, so pin bg to the (nudged) theme background -> invisible fill,
        # colored text only. Pinning bg also dodges the fg/bg invert bug (#817).
        theme_bg = resolve_bg("default", "#default")  # var(background)
        for u, ucolor in colors.items():
            scheme["rules"].append({
                "scope": "{}.{}".format(scope_prefix, u),
                "foreground": resolve_fg(u, ucolor),
                "background": theme_bg,
            })
    else:
        for u, ucolor in colors.items():
            for v, vcolor in colors.items():
                scheme["rules"].append({
                    "scope": "{}.{}.{}".format(scope_prefix, u, v),
                    "foreground": resolve_fg(u, ucolor),
                    "background": resolve_bg(v, vcolor),
                })

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(scheme, f, indent=4 if pretty else None)
    return path, len(scheme["rules"])
