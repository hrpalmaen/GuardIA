"""Local adapters that stand in for AWS services during development and demos.

Each adapter exposes the same interface as its boto3 client and is injected
through the handlers' `Deps`, so production code never imports this package.
Requests are validated against the botocore service model, and responses keep
the real API shape, so swapping a local adapter for the real client needs no
code changes.
"""
