"""Owns the protocols the application calls out through.

Names LlmSession, TextToSpeech, SpeechToText, DeckStore, RunStore, Tools and
Clock, and imports only `presentator.contracts`, because a port describes a
capability in domain types and never in an implementation's vocabulary.
"""
