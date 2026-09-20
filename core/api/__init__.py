"""Token-authenticated REST API for iteratively building LayerNexus projects.

The package exposes a Django REST Framework layer mounted at ``/api/v1/`` that lets a
client (typically an AI agent) assemble projects one validated step at a time: create a
project, add sub-project components, create/attach parts, upload STL files, attach
documents and assign hardware. All composition goes through the Phase-1 edge model
(:class:`~core.models.composition.ProjectComponent` / ``ProjectPart``) with the cycle
guard, so the API survives the future Phase-6 contract cleanup of the legacy FKs.

Printing (print jobs, queue, printers, moonraker) is intentionally out of scope.
"""
