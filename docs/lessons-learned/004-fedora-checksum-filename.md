# Fedora CHECKSUM filename mismatch

Fedora's CHECKSUM file is named after the **compose** (`Fedora-Cloud-44-1.7`),
not the image (`Fedora-Cloud-Base-Generic-44-1.7`). The old code derived the
CHECKSUM name from the image basename, producing a URL that returns 404, so
verification was silently skipped.

The fix moves the checksum URL derivation to the distro profile (`sums_url`
callable) and uses `re.sub` to transform the image name into the compose
CHECKSUM name.
