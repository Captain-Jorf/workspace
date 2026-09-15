"""RETIRED — Meta Graph API publishing is disabled on purpose.

The page is published through the official Buffer API only
(build/buffer_publish.py, behind the Quality Supervisor and the
AUTO_PUBLISH_ENABLED repo variable). This module keeps its name so that any
stale reference fails loudly instead of publishing.
"""
import sys

if __name__ == "__main__":
    print("insta_publish.py is retired: Meta Graph API publishing is disabled. Use build/buffer_publish.py.")
    sys.exit(1)
