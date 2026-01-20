# Get clipboard image on Windows
Add-Type -AssemblyName System.Windows.Forms

$clipboard = [System.Windows.Forms.Clipboard]::GetImage()
if ($clipboard -ne $null) {
    $stream = New-Object System.IO.MemoryStream
    $clipboard.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $bytes = $stream.ToArray()
    $base64 = [Convert]::ToBase64String($bytes)
    $stream.Dispose()
    Write-Output "image/png"
    Write-Output $base64
} else {
    Write-Output "no_image"
}
