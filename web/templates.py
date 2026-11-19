"""Shared, autoescaped templates for all public pages."""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

templates = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent),
    autoescape=select_autoescape(["html"]),
)
