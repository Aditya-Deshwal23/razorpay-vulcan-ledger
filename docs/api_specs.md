
🔌 Vulcan Ledger API Specifications

The FastAPI backend exposes the following core endpoints to the Next.js frontend.

POST /api/batches/upload
Description: Accepts a CSV file, parses the records, and initiates the background reconciliation pipeline.
Payload: multipart/form-data (file)
Response: Returns the batch_id and original_file_name.
GET /api/batches/{batch_id}/summary
Description: Polled continuously by the frontend (every 1 second) to fetch live metric updates during processing.
Response:
{
  "batch_id": "uuid",
  "original_file_name": "test_batch_60_comprehensive.csv",
  "total_records": 60,
  "resolved_count": 36,
  "exceptions_count": 24,
  "status": "processing"
}
DELETE /api/batches/{batch_id}
Description: Hard-deletes a batch. Cascades to remove all associated Ledger, AuditEvents, and Settlement rows to maintain a clean testing environment.
GET /api/audit?batch_id={batch_id}
Description: Retrieves the immutable ledger events for the export functionality.
Note: The frontend maps this data to export a file named audited_{original_file_name}.csv.
