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
