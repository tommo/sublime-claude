#!/bin/bash
# Get clipboard image on Linux (X11 or Wayland)

# Try Wayland first
if command -v wl-paste &> /dev/null && [ -n "$WAYLAND_DISPLAY" ]; then
    data=$(wl-paste --type image/png 2>/dev/null | base64 -w 0)
    if [ -n "$data" ]; then
        echo "image/png"
        echo "$data"
        exit 0
    fi
fi

# Try X11
if command -v xclip &> /dev/null; then
    data=$(xclip -selection clipboard -t image/png -o 2>/dev/null | base64 -w 0)
    if [ -n "$data" ]; then
        echo "image/png"
        echo "$data"
        exit 0
    fi
fi

echo "no_image"
