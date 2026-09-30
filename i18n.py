"""Explicit language selection and serializable gettext messages."""

import argparse
import gettext
from pathlib import Path


LANGUAGES = ("en", "it")
LOCALES = Path(__file__).resolve().parent / "locales"
_translation = gettext.NullTranslations()


def set_language(language):
    global _translation
    if language not in LANGUAGES:
        raise ValueError("language must be en or it")
    _translation = gettext.translation("keepass-sync", LOCALES, languages=[language],
                                       fallback=True) if language != "en" else gettext.NullTranslations()


class Message(str):
    """A rendered string retaining its English template and named arguments."""

    def __new__(cls, template, arguments=None):
        arguments = arguments or {}
        text = _translation.gettext(template)
        if arguments:
            text = text.format(**arguments)
        value = super().__new__(cls, text)
        value.template = template
        value.arguments = arguments
        return value

    def format(self, **arguments):
        return Message(self.template, arguments)


def _(template):
    return Message(template)


def message_fields(value):
    if isinstance(value, Message):
        return {"message_id": value.template,
                "message_args": {key: message_fields(arg) if isinstance(arg, Message) else arg
                                 for key, arg in value.arguments.items()}}
    return {"message": str(value)}


def render(record, fallback=""):
    """Render structured messages, accepting legacy records containing only text."""
    if not isinstance(record, dict):
        return str(record)
    template, arguments = record.get("message_id"), record.get("message_args", {})
    if isinstance(template, str) and isinstance(arguments, dict):
        try:
            return Message(template, {key: render(arg) if isinstance(arg, dict) else arg
                                      for key, arg in arguments.items()})
        except (KeyError, ValueError, IndexError, AttributeError):
            pass
    return str(record.get("message", fallback))


def select_cli_language(argv):
    """Select the explicit CLI language before configuration and help processing."""
    set_language("en")
    configure_argparse()
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--language", choices=LANGUAGES)
    args, _unknown = parser.parse_known_args(argv)
    set_language(args.language or "en")
    return args.language


def configure_argparse():
    # argparse's own help headings and usage errors use this gettext hook.
    argparse._ = lambda text: _translation.gettext(text)


def N_(text):
    """Mark argparse templates for extraction without translating at import time."""
    return text


# argparse's public-facing templates vary slightly across Python versions.
ARGPARSE_MESSAGES = (
    N_("usage: "), N_("positional arguments"), N_("optional arguments"), N_("options"),
    N_("show this help message and exit"), N_("invalid choice: %(value)r (choose from %(choices)s)"),
    N_("argument %(argument_name)s: %(message)s"), N_("%(prog)s: error: %(message)s\n"),
    N_("unrecognized arguments: %s"), N_("the following arguments are required: %s"),
    N_("expected one argument"), N_("not allowed with argument %s"),
)


def add_language_option(parser, suppress_default=False):
    parser.add_argument("--language", choices=LANGUAGES,
                        default=argparse.SUPPRESS if suppress_default else None,
                        help=_("Message language (default: configuration, then English)"))
