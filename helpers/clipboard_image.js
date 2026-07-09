// JXA script to get clipboard image
ObjC.import('AppKit');
ObjC.import('Foundation');

var pb = $.NSPasteboard.generalPasteboard;
var types = pb.types;

function output(str) {
    $.NSFileHandle.fileHandleWithStandardOutput.writeData(
        $.NSString.alloc.initWithString(str + '\n').dataUsingEncoding($.NSUTF8StringEncoding)
    );
}

// If clipboard has file URLs (e.g. copied from Finder), extract full paths.
// Finder copies include a thumbnail icon as public.tiff/public.png which we don't want.
if (types.containsObject('NSFilenamesPboardType')) {
    var plist = pb.propertyListForType('NSFilenamesPboardType');
    if (plist && plist.count > 0) {
        output('file_paths');
        for (var i = 0; i < plist.count; i++) {
            output(plist.objectAtIndex(i).js);
        }
        ObjC.import('stdlib');
        $.exit(0);
    }
}
if (types.containsObject('public.file-url')) {
    var urlStr = pb.stringForType('public.file-url');
    if (urlStr) {
        var url = $.NSURL.URLWithString(urlStr);
        if (url && url.isFileURL) {
            output('file_paths');
            output(url.path.js);
            ObjC.import('stdlib');
            $.exit(0);
        }
    }
}

// Prefer native image types (screenshots, browser "Copy Image", Preview).
// Order: PNG → JPEG → TIFF (convert) → generic NSImage pasteboard.
function emitPngFromData(data) {
    if (!data || data.length === 0) return false;
    var base64 = data.base64EncodedStringWithOptions(0).js;
    if (!base64) return false;
    output('image/png');
    output(base64);
    return true;
}

function emitJpegFromData(data) {
    if (!data || data.length === 0) return false;
    var base64 = data.base64EncodedStringWithOptions(0).js;
    if (!base64) return false;
    output('image/jpeg');
    output(base64);
    return true;
}

function tiffOrBitmapToPng(data) {
    if (!data || data.length === 0) return false;
    var bitmap = $.NSBitmapImageRep.imageRepWithData(data);
    if (!bitmap) return false;
    var pngData = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
    return emitPngFromData(pngData);
}

// public.png / Apple PNG
if (types.containsObject('public.png') || types.containsObject('Apple PNG pasteboard type')) {
    var pngType = types.containsObject('public.png') ? 'public.png' : 'Apple PNG pasteboard type';
    if (emitPngFromData(pb.dataForType(pngType))) {
        ObjC.import('stdlib');
        $.exit(0);
    }
}

// public.jpeg
if (types.containsObject('public.jpeg') || types.containsObject('public.jpg')) {
    var jpgType = types.containsObject('public.jpeg') ? 'public.jpeg' : 'public.jpg';
    if (emitJpegFromData(pb.dataForType(jpgType))) {
        ObjC.import('stdlib');
        $.exit(0);
    }
}

// TIFF / screenshot (convert → PNG)
if (types.containsObject('public.tiff') || types.containsObject('NeXT TIFF v4.0 pasteboard type')) {
    var tiffType = types.containsObject('public.tiff') ? 'public.tiff' : 'NeXT TIFF v4.0 pasteboard type';
    if (tiffOrBitmapToPng(pb.dataForType(tiffType))) {
        ObjC.import('stdlib');
        $.exit(0);
    }
}

// NSImage / generic image pasteboard (last resort)
try {
    var nsimg = $.NSImage.alloc.initWithPasteboard(pb);
    if (nsimg && nsimg.isValid) {
        var tiffData = nsimg.TIFFRepresentation;
        if (tiffOrBitmapToPng(tiffData)) {
            ObjC.import('stdlib');
            $.exit(0);
        }
    }
} catch (e) {
    // ignore
}

output('no_image');
