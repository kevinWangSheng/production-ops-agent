# Issue 290 ambiguities

- The contract says the shared resolver has two in-flight slots, but does not
  prescribe the public module or class that owns the pool. These tests observe
  only `getaddrinfo` concurrency and post-release thread settling.
- A query waiting for a slot is allowed to consume the remainder of its own
  request deadline. The tests use a 0.8 second deadline for stuck calls and
  allow the stated one second margin.
- IPv6 coverage depends on the runner having a listening `::1`; the IPv6 case
  is skipped when that loopback is unavailable.
- Thread-count assertions are made after releasing all intentionally blocked
  resolver calls and settling for up to three seconds, so the baseline includes
  pytest and the local HTTP server's normal threads.
