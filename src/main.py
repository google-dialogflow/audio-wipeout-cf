# main.py
import os
import functions_framework
from datetime import datetime
from google.cloud import bigquery
from google.cloud import storage

# --- Static Brand Configurations ---
# Central place to manage settings for each brand.
# To add a new brand, add a new configuration dictionary here, and spin up a new cloud scheduler job
# that points to this function with the appropriate brand in the request payload.
MCDONALDS_CONFIG = {
    "TRANSCRIPTS_TABLE_PATH": "mcdonalds-ttm.transcripts_decibel.prod",
    "BUCKET_NAME": "mcdonalds-ttm-audio",
    "WIPEOUT_LOG_TABLE_PATH": "foodai-analytics.audio_wipeout_dataset.mcdonalds_prod_log",
    "GCS_PATH_TEMPLATE": "mcdonalds-ttm/{agent_id}/{date_str}/{session_id}/",
}

# Master dictionary to look up brand configurations
BRAND_CONFIGS = {
    "mcdonalds": MCDONALDS_CONFIG,
}


# --- Initialize Google Cloud clients ---
# These clients are initialized globally to be reused across function invocations.
try:
    bq_client = bigquery.Client()
    storage_client = storage.Client()
except Exception as e:
    print(f"Error initializing Google Cloud clients: {e}")
    bq_client = None
    storage_client = None


@functions_framework.http
def fetch_redacted_transcripts_and_delete_audio(request):
    """
    An HTTP-triggered Cloud Function that processes transcripts for a specific brand.
    It identifies sessions with a '[redacted]' keyword and deletes corresponding
    audio folders from GCS if a bucket is configured for that brand.

    Expects a JSON payload in the request: {"brand": "wendys"}
    """
    if not bq_client or not storage_client:
        error_msg = "Cloud clients are not initialized. The function cannot proceed."
        print(f"ERROR: {error_msg}")
        return (error_msg, 500)

    # --- Get Brand from Request and Load Configuration ---
    request_json = request.get_json(silent=True)
    if not request_json or "brand" not in request_json:
        return ("Bad Request: Missing JSON payload with 'brand' key.", 400)

    brand = request_json["brand"].lower()
    config = BRAND_CONFIGS.get(brand)

    if not config:
        return (
            f"Configuration Error: Invalid brand '{brand}'. Available brands: {list(BRAND_CONFIGS.keys())}",
            400,
        )

    transcipts_table_path = config.get("TRANSCRIPTS_TABLE_PATH")
    gcs_bucket_name = config.get("BUCKET_NAME")
    log_table_path = config.get("WIPEOUT_LOG_TABLE_PATH")
    gcs_path_template = config.get("GCS_PATH_TEMPLATE")

    if not transcipts_table_path:
        return (
            f"Configuration Error: 'TRANSCRIPTS_TABLE_PATH' not set for brand '{brand}'.",
            500,
        )

    print(f"Starting job for brand: '{brand}'")

    # --- Construct and Execute BigQuery Query ---
    # This query now joins against a log table to exclude already processed sessions.
    join_clause = ""
    where_clause_addition = ""
    if log_table_path:
        print(
            f"Using log table to exclude previously deleted sessions: `{log_table_path}`"
        )
        join_clause = (
            f"LEFT JOIN `{log_table_path}` log ON T.session_id = log.session_id"
        )
        where_clause_addition = "AND log.session_id IS NULL"

    print(f"Scanning table: `{transcipts_table_path}` for the last 60 minutes.")
    print(
        f"Target GCS Bucket: `{gcs_bucket_name or 'Not configured (deletion skipped)'}`"
    )

    # --- Construct and Execute BigQuery Query ---
    query = f"""
        WITH ConversationTurn AS (
          SELECT
            request_time,
            SPLIT(conversation_name, "/")[OFFSET(1)] AS project_id,
            SPLIT(conversation_name, "/")[OFFSET(5)] AS agent_id,
            SPLIT(conversation_name, "/")[OFFSET(7)] AS session_id,
            STRING(derived_data.userUtterances) AS customer_utterance
          FROM
            `{transcipts_table_path}`
          WHERE
            request_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 60 MINUTE)
        )
        SELECT DISTINCT
            T.session_id,
            T.project_id,
            T.agent_id,
            T.request_time
        FROM
            ConversationTurn AS T
            {join_clause}
        WHERE
            T.customer_utterance LIKE '%[redacted]%'
            AND T.session_id IS NOT NULL
            AND T.agent_id IS NOT NULL
            AND T.request_time IS NOT NULL
            {where_clause_addition}
    """

    deleted_folder_count = 0
    deleted_file_count = 0
    scanned_session_count = 0
    error_count = 0

    try:
        print(f"Executing query:\n{query}")
        query_job = bq_client.query(query)

        for row in query_job:
            scanned_session_count += 1
            session_id, project_id, agent_id, request_time = (
                row["session_id"],
                row["project_id"],
                row["agent_id"],
                row["request_time"],
            )

            # --- GCS Deletion Logic (Conditional) ---
            if not gcs_bucket_name or not gcs_path_template:
                # If no bucket or template is configured, we only log the finding and do not delete.
                print(
                    f"INFO: Found session {session_id} for brand '{brand}', but GCS deletion is disabled (missing bucket or path template)."
                )
                continue

            date_str = request_time.strftime("%Y-%m-%d")
            gcs_folder_prefix = gcs_path_template.format(
                agent_id=agent_id, date_str=date_str, session_id=session_id
            )
            print(
                f"Found session to delete: {session_id}. Targeting GCS file path: gs://{gcs_bucket_name}/{gcs_folder_prefix}"
            )

            try:
                bucket = storage_client.bucket(gcs_bucket_name)
                blobs_to_delete = list(bucket.list_blobs(prefix=gcs_folder_prefix))

                if not blobs_to_delete:
                    print(
                        f"WARN: No files found in GCS with prefix '{gcs_folder_prefix}'. This could be correct if the folder was already deleted."
                    )
                    continue

                print(f"  - Found {len(blobs_to_delete)} file(s) to delete.")
                for blob in blobs_to_delete:
                    blob.delete()
                    deleted_file_count += 1

                deleted_folder_count += 1
                print(f"SUCCESS: Deleted all files for session {session_id}.")

                # --- Log the successful deletion to BigQuery ---
                if log_table_path:
                    rows_to_insert = [
                        {
                            "session_id": session_id,
                            "request_timestamp": request_time.isoformat(),
                            "deleted_timestamp": datetime.utcnow().isoformat(),
                        }
                    ]
                    insert_errors = None
                    bq_client.insert_rows_json(log_table_path, rows_to_insert)
                    if not insert_errors:
                        print(
                            f"  - Successfully logged session {session_id} to wipeout log."
                        )
                    else:
                        print(
                            f"  - ERROR: Failed to log session {session_id} to wipeout log: {insert_errors}"
                        )
                        error_count += 1

            except Exception as e:
                print(
                    f"ERROR: Failed to delete files in '{gcs_folder_prefix}'. Reason: {e}"
                )
                error_count += 1

    except Exception as e:
        print(f"An unexpected error occurred during BigQuery query or processing: {e}")
        return (f"Job failed for brand '{brand}' with error: {e}", 500)

    summary_message = (
        f"Job finished for brand '{brand}'. Found {scanned_session_count} sessions matching criteria. "
        f"Successfully deleted {deleted_folder_count} folders ({deleted_file_count} total files). "
        f"Encountered {error_count} errors during GCS deletion."
    )
    print(summary_message)
    return (summary_message, 200)
