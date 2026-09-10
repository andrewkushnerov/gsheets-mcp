"""gsheets-mcp — a self-hosted MCP server that gives Claude your Google Sheets."""

#: The one place the version is written by hand. The release workflow reads this
#: literal to name the tag and cut the release, so the tag is an output of a release
#: rather than a second place to type the number. It lives in this module because
#: this module has no imports: CI reads it from a bare checkout without installing
#: a single dependency, which ``protocol.py`` (pydantic, googleapiclient) could not do.
__version__ = "0.3.0"
