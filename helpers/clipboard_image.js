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

// Check for PNG
if (types.containsObject('public.png')) {
    var data = pb.dataForType('public.png');
    if (data && data.length > 0) {
        var base64 = data.base64EncodedStringWithOptions(0).js;
        output('image/png');
        output(base64);
        ObjC.import('stdlib');
        $.exit(0);
    }
}

// Check for TIFF and convert to PNG
if (types.containsObject('public.tiff')) {
    var data = pb.dataForType('public.tiff');
    if (data && data.length > 0) {
        var bitmap = $.NSBitmapImageRep.imageRepWithData(data);
        if (bitmap) {
            var pngData = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $());
            if (pngData && pngData.length > 0) {
                var base64 = pngData.base64EncodedStringWithOptions(0).js;
                output('image/png');
                output(base64);
                ObjC.import('stdlib');
                $.exit(0);
            }
        }
    }
}

output('no_image');
