"""
pipeline/sandbox/backends — concrete launcher implementations.

One module per backend so platform-specific imports stay local.
The public selector lives in :mod:`pipeline.sandbox.launcher` so
callers never import a backend directly.
"""
