#!/usr/bin/env python3
"""Helper script to get image from macOS clipboard.

Outputs:
  - "image/png\n<base64_data>" if image found
  - "no_image" if no image
  - "error:<message>" on error
"""
import sys
import base64

try:
    from AppKit import NSPasteboard, NSPasteboardTypePNG, NSPasteboardTypeTIFF, NSBitmapImageRep, NSPNGFileType

    pasteboard = NSPasteboard.generalPasteboard()
    types = pasteboard.types()

    # Try PNG first
    if NSPasteboardTypePNG in types:
        data = pasteboard.dataForType_(NSPasteboardTypePNG)
        if data:
            print("image/png")
            print(base64.b64encode(bytes(data)).decode())
            sys.exit(0)

    # Try TIFF and convert to PNG
    if NSPasteboardTypeTIFF in types:
        data = pasteboard.dataForType_(NSPasteboardTypeTIFF)
        if data:
            bitmap = NSBitmapImageRep.imageRepWithData_(data)
            if bitmap:
                png_data = bitmap.representationUsingType_properties_(NSPNGFileType, None)
                if png_data:
                    print("image/png")
                    print(base64.b64encode(bytes(png_data)).decode())
                    sys.exit(0)

    print("no_image")

except Exception as e:
    print(f"error:{e}")
    sys.exit(1)
