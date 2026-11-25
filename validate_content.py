"""Startup validation for content and cloud configuration."""
import os
import sys

from cloud_sync import validate_config
from content import thumbnail_offset

try:
    validate_config()
    thumbnail_offset(os.environ.get('YOUTUBE_THUMBNAIL_TIMESTAMP', '20%'), 3600)
except ValueError as error:
    print(str(error), file=sys.stderr)
    raise SystemExit(1) from None
