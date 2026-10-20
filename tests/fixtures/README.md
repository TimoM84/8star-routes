# Test fixtures

`synthetic_*.json` files are **synthetic**. They were written by hand to match the
documented shape of TomTom Routing API `calculateRoute` responses, because the
TomTom API was not reachable from the development environment. They are not
recorded provider responses.

Recorded responses go in `recorded/`, written by `scripts/live_verify.py`.
That script strips the API key and stores only the response body and selected headers.
