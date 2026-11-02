"""Apply Linux resource limits before exec; safe to launch from a threaded server."""
import os
import resource
import sys

resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024**2,) * 2)
resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.execv(sys.argv[1], sys.argv[1:])
