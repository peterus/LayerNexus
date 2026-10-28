"""Token-authenticated REST API for iteratively building LayerNexus projects.

The package exposes a Django REST Framework layer mounted at ``/api/v1/`` that lets a
client (typically an AI agent) assemble projects one validated step at a time: create a
top-level project, add sub-projects (``parent`` + ``quantity``), create parts inside a
project, upload STL files, attach documents and assign hardware. Projects form a strict
tree; re-parenting is validated against cycles.

Printing (print jobs, queue, printers, moonraker) is intentionally out of scope.
"""
