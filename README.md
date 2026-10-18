# Signalpost Company Research Agent

An AI-powered company research agent built with Python, SQLite, the official Brønnøysund Register Centre API, and Ollama. The system collects Norwegian company information, stores company facts, tracks changes over time, and allows users to ask questions about stored company records using natural language.

## Overview

The Signalpost Company Research Agent is designed to automate company information collection and make stored business data easier to explore.

The system retrieves company records from the official Norwegian business registry, stores the information in a local SQLite database, maintains a history of fact changes, and provides an interactive AI assistant for querying company information.

## Features

- **Company Data Collection:** Retrieves Norwegian company information from the official Brønnøysund Register Centre API.
- **Database Storage:** Stores company records and facts in SQLite.
- **Company Research:** Collects information such as organization number, company name, address, industry, website, and registration details when available.
- **Fact Change Tracking:** Records changes to stored company facts for historical reference.
- **Data Refresh:** Refreshes existing records and discovers additional company candidates.
- **AI-Powered Questions:** Allows users to ask natural-language questions about companies in the database.
- **Local AI Model:** Uses Ollama with the `qwen2.5:1.5b` model to generate responses.
- **Interactive CLI:** Provides a command-line interface for asking multiple questions in one session.

## Enhanced AI Feature

The project includes an AI-powered question-answering assistant integrated with **Ollama and the `qwen2.5:1.5b` language model**.

Users can ask questions about companies stored in the local database without writing SQL queries manually.

### Key Capabilities

- **Natural Language Queries:** Ask questions such as "What information do you have about this company?"
- **SQLite Integration:** Retrieves relevant company records and facts from `companies.db`.
- **AI-Generated Responses:** Uses Ollama to turn retrieved company information into readable answers.
- **Local Inference:** Runs the language model locally instead of requiring a paid hosted LLM API.
- **Interactive Session:** Supports repeated queries until the user types `exit`.
- **Source Information:** Can include source URLs when they are present in the retrieved company records.

### AI Query Workflow

1. The user enters a company-related question.
2. The assistant searches the stored company records and facts.
3. Relevant information is selected from the SQLite database.
4. The retrieved information is passed to Ollama.
5. The model generates a natural-language response.
6. The answer is displayed in the terminal.

### Example

```text
SIGNALPOST COMPANY RESEARCH ASSISTANT

Ask questions about companies in your database.
Type 'exit' to quit.

Companies loaded: 1080
Ollama model: qwen2.5:1.5b

Your question: What information do you have about Equinor?

Searching stored company information...

Answer:
[AI-generated answer based on retrieved company information]
```

The number of companies shown depends on the current contents of the local database.

### Accuracy Considerations

The AI assistant depends on the quality of the company records retrieved from the database. A relevant-looking result does not necessarily mean that the correct company has been selected.

Planned improvements include:

- Prioritizing exact company-name matches.
- Verifying organization numbers against the official registry.
- Asking the user to choose when multiple companies have similar names.
- Keeping facts associated with the correct organization number.
- Reducing unsupported AI-generated claims.
- Displaying official source URLs for verification.

These improvements should be considered implemented only after the corresponding code has been added and tested.

## Technology Stack

| Component | Technology |
|---|---|
| Programming language | Python |
| Database | SQLite |
| HTTP client | Requests |
| Company data source | Brønnøysund Register Centre API |
| Local AI runtime | Ollama |
| Language model | `qwen2.5:1.5b` |
| Interface | Command-line interface |
| Data format | JSON |

## Data Source

The project uses the official Brønnøysund Register Centre API to retrieve Norwegian company information.

- API documentation: https://data.brreg.no/enhetsregisteret/api/dokumentasjon/
- Company registry API: https://data.brreg.no/enhetsregisteret/api/enheter

Company details may include organization number, registered name, organization type, business address, industry classification, website, employee count, registration dates, and certain business status fields, depending on availability in the registry.

## Database Structure

The SQLite database stores company information and supports historical tracking.

### `companies`

Stores the main company records, including organization number, company name, source URL, and retrieval timestamp.

### `company_facts`

Stores individual company facts linked to the corresponding organization number, together with their values, source URLs, and retrieval timestamps.

### `company_fact_history`

Maintains a history of fact changes, including the fact name, change type, previous value, new value, source URL, and retrieval timestamp.

The exact columns depend on the database schema used by the current version of the project.

## Project Structure

```text
signalpost-agent/
├── main.py              # Company data collection and refresh
├── ask_company.py       # Interactive AI company research assistant
├── requirements.txt     # Python dependencies
├── companies.db         # SQLite database
├── check_db.py          # Database inspection utility
├── inspect_data.py      # Stored data inspection utility
├── quality_check.py     # Optional data quality checks
└── README.md            # Project documentation
```

Only include optional files such as `quality_check.py` if they exist in your repository.

## Installation

### Prerequisites

- Python 3.10 or a compatible Python version.
- Internet access for retrieving registry data.
- Ollama installed locally for the AI assistant.
- Git, if you want to clone the repository.

### 1. Clone the repository

```bash
git clone https://github.com/2116240701394/signalpost-company-research-agent.git
cd signalpost-company-research-agent
```

### 2. Install Python dependencies

```bash
python -m pip install -r requirements.txt
```

If you do not have a `requirements.txt` file yet, install Requests directly:

```bash
python -m pip install requests
```

### 3. Run the company research agent

```bash
python main.py
```

This runs the company collection and refresh workflow and updates the local database according to the script's configuration.

### 4. Install and configure Ollama

Download Ollama from https://ollama.com/.

Pull the language model:

```bash
ollama pull qwen2.5:1.5b
```

Make sure the Ollama service is running. On Windows, Ollama may already be running in the background; if so, you do not need to start a second server.

You can check available models with:

```bash
ollama list
```

### 5. Start the AI assistant

Ensure that `companies.db` exists and contains company records, then run:

```bash
python ask_company.py
```

## Example Questions

Try asking questions such as:

```text
What information do you have about Equinor?
```

```text
What is the industry and website of this company?
```

```text
Find a company using its organization number.
```

```text
What company information is available in the database?
```

The answers depend on the records available and the retrieval logic implemented in `ask_company.py`.

Type `exit` to close the interactive assistant.

## Testing and Validation

The project can be checked using the following steps:

1. Run `main.py` and confirm that registry data is collected or refreshed.
2. Inspect `companies.db` to confirm that company records and facts are stored.
3. Run `ask_company.py` and confirm that the assistant loads database records.
4. Submit a company-related question and check that an answer is generated.
5. Compare the returned organization number and company details against the official registry.
6. Test an unknown company name and confirm that the assistant does not invent company records.

A successful AI response alone does not guarantee that the correct company record was retrieved.

## Limitations

- The database contains only the company records collected by the agent.
- The completeness of the information depends on the source registry and the data collection process.
- The AI assistant may retrieve an incorrect or incomplete match if company-name matching is ambiguous.
- A local language model may generate unsupported details if the retrieved context is insufficient.
- Registry availability and network connectivity can affect data retrieval.
- Historical records reflect changes observed by the agent; they are not necessarily a complete history of every change in the official registry.

## Future Enhancements

- Improve exact and fuzzy company-name matching.
- Verify company identity and facts directly against the official registry.
- Add clarification prompts for ambiguous company names.
- Introduce stronger source-grounding and answer validation.
- Add filters for industry, employee count, registration status, and location.
- Support structured company comparisons and database-wide analytics.
- Develop a web-based dashboard for company search and research.
- Add automated testing and data-quality reports.
- Explore semantic search for more flexible question retrieval


