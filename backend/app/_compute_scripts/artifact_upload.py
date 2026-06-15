"""Artifact uploader invoked by the GCP compute-job VM startup script.

argv[1]: signed PUT URL for the artifact bundle
argv[2]: local path to the .tar.gz bundle to upload
"""

import pathlib
import sys
import urllib.request

url = sys.argv[1]
path = pathlib.Path(sys.argv[2])
request = urllib.request.Request(
    url,
    data=path.read_bytes(),
    method="PUT",
    headers={"Content-Type": "application/gzip"},
)
with urllib.request.urlopen(request, timeout=120) as response:
    response.read()
