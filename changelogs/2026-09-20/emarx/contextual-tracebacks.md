Background deployment errors now show numbered source context around the function
definition, with a marker on the relevant line. Unexpected errors and exceptions
logged through `configure_logging()` include the same context for each stack
frame, preserving exception chains, notes, and groups without requiring Rich or
any new dependencies. Interactive terminals highlight source locations, failing
lines and expressions, and exception types with color. Redirected output stays
plain, and the existing `NO_COLOR`, CI, notebook, and logging color settings are
respected.
