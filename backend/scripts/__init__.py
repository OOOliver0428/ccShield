"""ccShield backend scripts.

Operational scripts invoked via ``python -m scripts.<name>``. Each script is
self-contained, may import the application (``app.*``) freely, but must
not be imported by the application at runtime — the package exists so
``-m`` can resolve ``scripts.capture_fixtures``.

Currently ships:
    - capture_fixtures: snapshot B站 API responses for offline test replay,
      with mandatory credential redaction before anything is written to disk.
"""
