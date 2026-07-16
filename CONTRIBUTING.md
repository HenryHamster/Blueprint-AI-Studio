# Contributing to Blueprint

Thanks for your interest in contributing to Blueprint! This guide will help you get started.

## Development Environment Setup

### Prerequisites

- Node.js v18+ (v20.x recommended; production runners run Node 20)
- Python 3.11+
- Docker and Docker Compose
- A GCP project (for infrastructure work)

### Getting Started

1. **Clone the repository:**

   ```bash
   git clone <repo-url>
   cd blueprint
   ```

2. **Copy the environment file and fill in your values:**

   ```bash
   cp .env.example .env
   ```

3. **Start the backend services (PostgreSQL + FastAPI):**

   ```bash
   docker-compose up -d
   ```

4. **Install frontend dependencies and start the dev servers:**

   ```bash
   npm install
   npm run dev
   ```

   This starts the main frontend on port 3000 and the admin UI on port 3001.

5. **Install backend Python dependencies (for local development outside Docker):**

   ```bash
   cd backend
   pip install -r requirements.txt
   ```

6. **Run database migrations:**

   ```bash
   cd backend
   alembic upgrade head
   ```

## Submitting Pull Requests

1. Create a feature branch from `main`:

   ```bash
   git checkout -b your-feature-name main
   ```

2. Make your changes in small, focused commits with clear messages.

3. Run linting and tests before pushing:

   ```bash
   # Frontend (admin-ui) — build check + lint
   cd admin-ui && npm run build && npx eslint .

   # Backend — runs full test suite in Docker
   bash scripts/test-backend.sh

   # Agent — TypeScript type check
   cd agent && npx tsc --noEmit
   ```

4. Push your branch and open a pull request against `main`.

5. In your PR description, include:
   - A summary of what changed and why.
   - Steps to test the changes.
   - Screenshots for any UI changes.

6. Address review feedback by pushing additional commits (don't force-push).

## Coding Standards

### General

- Write clear, self-documenting code. Add comments only where the intent isn't obvious.
- Keep pull requests focused on a single concern.

### TypeScript / React (Frontend & Agent)

- Follow the existing ESLint configuration.
- Use TypeScript strict mode — avoid `any` where possible.
- Use functional components and hooks for React code.

### Python (Backend)

- Follow PEP 8 conventions.
- Use type hints for function signatures.
- Write async handlers where applicable (FastAPI is async-first).
- Add tests for new endpoints and business logic using pytest.

### Running the blueprint-runner locally

The agent runner (`agent/`) requires Node.js >=18.0.0; production runners use
Node 20.x. The runner checks its Node version at start-up and exits immediately
with an actionable message if the requirement is not met.

To install a supported Node version via [nvm](https://github.com/nvm-sh/nvm):

```bash
nvm install 20
nvm use 20
node --version   # should print v20.x.x
cd agent && npm install && npm start
```

### Infrastructure

- Document any new Terraform variables or resources.
- Test Packer builds locally before submitting changes to VM image configs.
