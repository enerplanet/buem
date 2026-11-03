Development Setup
=================

Setting up BuEM for development and contribution.

Development Prerequisites
-------------------------

See :doc:`prerequisites` for the full software requirements table.

* Git
* Python >= 3.13
* Conda
* IDE with Python support (VS Code or PyCharm recommended)

Development Installation
------------------------

.. code-block:: bash

    # Fork and clone the repository
    git clone https://github.com/your-username/buem.git
    cd buem
    
    # Create development environment
    conda env create -f infrastructure/env/buem_env.yml
    conda activate buem_env
    
    # Install dev dependencies (matches pyproject.toml's [dev] extra)
    pip install -e .[dev]   # mypy, pytest, pytest-cov, ruff

    # Install pre-commit hooks (optional)
    # pip install pre-commit
    # pre-commit install

.. note::
   **Important:** When working with the conda environment, always use ``python -m src.buem.main`` 
   to run BUEM commands to avoid import path conflicts. The source code structure requires 
   the ``src.`` prefix when importing modules directly from the repository.

Code Quality Tools
------------------

**Linting and Type Checking** (matches ``.github/workflows/ci.yml`` exactly):

.. code-block:: bash

    # Lint
    ruff check src/ tests/

    # Type check
    mypy src

    # Auto-fix what ruff can fix
    ruff check --fix src/ tests/

Testing Framework
-----------------

**Running Tests:**

.. code-block:: bash

    # Run all tests
    pytest
    
    # Run with coverage
    pytest --cov=buem --cov-report=html
    
    # Run specific test file
    pytest tests/test_api.py
    
    # Run tests with verbose output
    pytest -v

**Test Structure** (representative, not exhaustive — see ``tests/`` for
the current full list):

.. code-block:: text

    tests/
    ├── test_geojson_integration.py   # GeoJSON validation/processing pipeline
    ├── test_building_types.py        # residential + services-building end-to-end
    ├── test_attribute_builder_strictness.py  # required-attribute/fallback behavior
    ├── test_cache.py                 # result cache
    └── test_cli.py                   # CLI smoke tests

Building Documentation
----------------------

.. code-block:: bash

    # Ensure conda environment is active
    conda activate buem_env
    
    # Build documentation
    cd docs
    make html
    
    # Serve locally
    python -m http.server 8000 -d build/html
    
    # Clean build
    make clean

Contribution Workflow
---------------------

1. **Create Feature Branch:**

.. code-block:: bash

    git fetch origin
    git checkout -b feature/your-feature-name origin/dev

2. **Make Changes and Test:**

.. code-block:: bash

    # Edit code
    # Add tests
    pytest
    
3. **Lint and Type Check:**

.. code-block:: bash

    ruff check src/ tests/
    mypy src

4. **Commit and Push:**

.. code-block:: bash

    git add .
    git commit -m "Add feature: description"
    git push origin feature/your-feature-name

5. **Create Pull Request** against ``dev``. ``enerplanet`` is the release branch and only receives merges from ``dev``.

Debugging Configuration
-----------------------

**VS Code Settings:**

Create ``.vscode/settings.json``:

.. code-block:: json

    {
        "python.defaultInterpreterPath": "./venv/bin/python",
        "python.testing.pytestEnabled": true,
        "python.testing.pytestArgs": ["tests/"]
    }

Ruff and mypy are best run via the CLI (or their own editor extensions)
rather than through legacy ``python.linting.*`` settings.