"""Build step: download sqlglot at the pinned commit from its GitHub repository."""
import io
import os
import sys
import tarfile
import urllib.request

sha = sys.argv[1]
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=180).read()
tarfile.open(fileobj=io.BytesIO(data)).extractall("/opt", filter="data")
os.rename(f"/opt/sqlglot-{sha}", "/opt/sqlglot")
print("fetched sqlglot", sha)
