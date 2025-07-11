# Audio Wipeout Cloud Function

This document describes an automated process for identifying and deleting specific audio recordings from Google Cloud Storage based on the content of their corresponding transcripts stored in BigQuery.

The primary use case is to automatically remove audio files for conversations where a user utterance contains a specific keyword (e.g., `[redacted]`), ensuring data privacy and compliance.

## Table of Contents
1.  [Process Overview](#process-overview)
2.  [Key Features](#key-features)
3.  [Local Development and Testing](#local-development-and-testing)
4.  [Configuration: Adding a New Brand](#configuration-adding-a-new-brand)
5.  [Core Logic Explained](#core-logic-explained)
    * [Implicit Retries](#implicit-retries)
    * [Idempotency (Preventing Duplicates)](#idempotency-preventing-duplicates)
6.  [Deployment via GitHub Actions](#deployment-via-github-actions)

---

## 1. Process Overview

The wipeout process is designed as a serverless, event-driven workflow orchestrated by Google Cloud services and deployed via CI/CD.

The flow is as follows:
1.  **Cloud Scheduler**: A scheduler job is configured to run every 15 minutes. It triggers the Cloud Function via an authenticated HTTP request, passing a JSON payload to specify which brand to process (e.g., `{"brand": "mcdonalds"}`).
2.  **Cloud Function**: The Python function receives the request and loads the configuration for the specified brand.
3.  **BigQuery Query**: The function executes a query against the brand's transcripts table. It specifically looks for conversations in the last **60 minutes** where the user's utterance contains `[redacted]`.
4.  **Filter Processed Sessions**: To prevent re-processing, the query joins against a log table (`*_wipeout_log`) and excludes any `session_id` that has already been successfully processed.
5.  **GCS Deletion**: For each new `session_id` found, the function constructs the corresponding folder path in Google Cloud Storage and deletes all audio files within that folder.
6.  **Log Deletion**: After a session's audio is successfully deleted, the function writes the `session_id` and a timestamp to the log table, marking it as complete.

![Process Flow Diagram](https://placehold.co/800x250/F0F4F8/334155?text=Scheduler+%E2%86%92+Function+%E2%86%92+BigQuery+%26+GCS+%E2%86%92+BigQuery+Log)

---

## 2. Key Features

* **Brand-Specific Configuration**: Easily manage settings for multiple brands from a single, static dictionary in the code.
* **Automated & Serverless**: Runs automatically on a schedule without any servers to manage.
* **Robust & Resilient**: Includes an implicit retry mechanism to handle transient failures.
* **Efficient & Idempotent**: Uses a log table to ensure that audio for a session is deleted only once, saving on cost and preventing errors.
* **Secure**: Deployed with settings that prevent unauthenticated access, and uses Workload Identity Federation for secure CI/CD authentication.

---

## 3. Local Development and Testing

To run and test the function on your local machine, you first need to authenticate with Google Cloud and set up a local Python environment.


> **⚠️ CAUTION: Running Locally Performs Real Deletions**
>
> When you run this function locally, it uses your `gcloud` authenticated user's permissions. This means it will connect to the **real BigQuery tables and GCS buckets** defined in your configuration.
>
> **Any deletion operations it performs are permanent.** It is strongly recommended to:
> * Use a separate, non-production GCP project for testing.
> * Or, temporarily point the configuration to test tables/buckets.
> * Or, be absolutely certain of the query and paths before running against production data.

### Prerequisites
* [Python 3.11 or later](https://www.python.org/downloads/) is installed.
* The [Google Cloud SDK](https://cloud.google.com/sdk/docs/install) is installed.

### Setup Instructions

1.  **Authenticate with Google Cloud:**
    Run this command in your terminal and follow the browser prompts to log in. This gives your local environment the necessary permissions to interact with BigQuery and GCS.
    ```bash
    gcloud auth application-default login
    ```

2.  **Create a Virtual Environment:**
    From the root of the project directory, create and activate a Python virtual environment. This isolates the project's dependencies.
    ```bash
    # Create the virtual environment
    python3 -m venv .venv

    # Activate it (on macOS/Linux)
    source .venv/bin/activate
    ```

3.  **Install Dependencies:**
    Install the required Python libraries from the `requirements.txt` file.
    ```bash
    pip3 install -r src/requirements.txt
    ```

4.  **Run the Function Locally:**
    The Functions Framework allows you to run your Cloud Function on a local web server that emulates the Google Cloud environment.
    Run the following command from inside the `src` directory**
    ```bash
    functions-framework --source=src/ --target=fetch_redacted_transcripts_and_delete_audio --port=8080
    ```

5.  **Test the Local Function:**
    Once the server is running, you can send a `POST` request to it using a tool like `curl` or Postman. This request mimics the one sent by Cloud Scheduler.
    ```bash
    curl -X POST http://localhost:8080 \
      -H "Content-Type: application/json" \
      -d '{"brand": "mcdonalds"}'
    ```
    You should see detailed log output in the terminal where the functions framework is running.

---

## 4. Configuration: Adding a New Brand

To add a new brand (e.g., "burgerking"), follow these steps:

1.  **Create a Log Table in BigQuery**:
    For each new brand that requires deletion, you must first create a log table to track processed sessions. Run the following SQL in your BigQuery workspace, replacing the table path with your new brand's details.
    ```sql
    CREATE TABLE `foodai-analytics.audio_wipeout_dataset.burgerking_prod_log`
    (
      session_id STRING,
      request_timestamp TIMESTAMP,
      deleted_timestamp TIMESTAMP
    )
    PARTITION BY DATE(deleted_timestamp);
    ```

2.  **Update `main.py`:**
    Open `main.py` and add a new configuration dictionary for the new brand inside the `BRAND_CONFIGS` dictionary.

    ```python
    # In main.py

    BURGERKING_CONFIG = {
        "TRANSCRIPTS_TABLE_PATH": "{ADD_PATH}", # Change to actual path
        "BUCKET_PATH_PREFIX": "{ADD_PATH}", # Change to actual path
        "WIPEOUT_LOG_TABLE_PATH": "{ADD_PATH}", # Path from step 1
    }

    BRAND_CONFIGS = {
        "mcdonalds": MCDONALDS_CONFIG,
        "burgerking": BURGERKING_CONFIG, # Add the new brand here
    }
    ```

3.  **Deploy and Schedule:**
    Commit the changes to your `main` branch. GitHub Actions will automatically deploy the updated function. After deployment, create a **new Cloud Scheduler job** that targets the function with the payload `{"brand": "burgerking"}`.

---

## 5. Core Logic Explained

### Implicit Retries

The system has a built-in, simple retry mechanism to guard against temporary issues (e.g., a brief network outage, a temporary GCS issue).

* The Cloud Scheduler job runs every **15 minutes**.
* However, the BigQuery query inside the function scans for data from the last **60 minutes**.

This creates a 45-minute overlapping window. If a run fails for any reason, the sessions from that run will still be within the 60-minute window for the next three scheduled runs, giving the system multiple opportunities to process them successfully.

### Idempotency (Preventing Duplicates)

It's critical that we don't try to delete the same session's audio multiple times.

This is achieved by using the `WIPEOUT_LOG_TABLE_PATH`.

1.  **Check Before Processing**: The main BigQuery query performs a `LEFT JOIN` against the log table. The `WHERE log.session_id IS NULL` clause effectively filters out any session that is already in the log.
2.  **Log After Processing**: As soon as the function successfully deletes the audio files for a session, it immediately writes that `session_id` to the log table.

This ensures that even with the overlapping window for retries, a session is only ever processed once.

---

## 6. Deployment via GitHub Actions

This project is configured for Continuous Deployment using GitHub Actions. The workflow is defined in `.github/workflows/ci_cd.yaml`.

* **Trigger**: The deployment workflow runs automatically whenever code is pushed to the `main` branch.
* **Steps**: The workflow checks out the code, authenticates to Google Cloud, and then uses the `gcloud` CLI to deploy the function with all the specified settings (runtime, memory, entry point, etc.).
