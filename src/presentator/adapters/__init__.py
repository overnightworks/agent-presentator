"""Owns every implementation of a port.

Holds the agent providers, webauth, text-to-speech, speech-to-text, SQLite, the
filesystem and the Slidev build, and imports `presentator.ports` and
`presentator.contracts` only -- never the application, the API, or the host.
"""
