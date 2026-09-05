"use client";

import { Upload, CheckCircle2, AlertCircle, Loader2, FileText } from "lucide-react";
import { useCallback, useRef, useState } from "react";

import { api } from "@/lib/api";
import type { BatchUploadResponse } from "@/types/api";

type UploadState =
  | { phase: "idle" }
  | { phase: "dragging" }
  | { phase: "uploading"; filename: string }
  | { phase: "success"; result: BatchUploadResponse }
  | { phase: "error"; message: string };

interface BatchUploaderProps {
  /** Called when the backend accepts a batch, with the payload hash and row count */
  onSuccess?: (batchId: string, records: number) => void;
  progress?: number;
  progressActive?: boolean;
}

export function BatchUploader({ onSuccess, progress = 0, progressActive = false }: BatchUploaderProps) {
  const [state, setState] = useState<UploadState>({ phase: "idle" });
  const inputRef = useRef<HTMLInputElement>(null);

  const processFile = useCallback(
    async (file: File) => {
      const name = file.name.toLowerCase();
      if (!name.endsWith(".csv") && !name.endsWith(".json")) {
        setState({ phase: "error", message: "Upload a settlement export as .csv or a JSON array (.json)." });
        return;
      }
      if (file.size > 10 * 1024 * 1024) {
        setState({ phase: "error", message: "File is too large (max 10 MB). Split the export into smaller batches." });
        return;
      }

      setState({ phase: "uploading", filename: file.name });
      try {
        const result = await api.upload(file);
        setState({ phase: "success", result });
        window.dispatchEvent(new Event("vulcan:batch-state-changed"));
        onSuccess?.(result.batch_id, result.records);
      } catch (err) {
        const message = err instanceof Error ? err.message : "Upload failed. Check the file format and try again.";
        setState({ phase: "error", message });
      }
    },
    [onSuccess],
  );

  const handleFiles = useCallback(
    (files: FileList | null) => {
      if (!files?.length) return;
      void processFile(files[0]);
    },
    [processFile],
  );

  const handleDrop = useCallback(
    (event: React.DragEvent) => {
      event.preventDefault();
      setState((prev) => (prev.phase === "dragging" ? { phase: "idle" } : prev));
      handleFiles(event.dataTransfer.files);
    },
    [handleFiles],
  );

  const handleDragOver = useCallback((event: React.DragEvent) => {
    event.preventDefault();
    setState({ phase: "dragging" });
  }, []);

  const handleDragLeave = useCallback(() => {
    setState((prev) => (prev.phase === "dragging" ? { phase: "idle" } : prev));
  }, []);

  function reset() {
    setState({ phase: "idle" });
    if (inputRef.current) inputRef.current.value = "";
  }

  if (state.phase === "success") {
    const { result } = state;
    return (
      <div className="uploader-result uploader-success">
        <CheckCircle2 size={22} aria-hidden="true" />
        <div className="uploader-result-body">
          <strong>
            Batch accepted &mdash; {result.records} record{result.records !== 1 ? "s" : ""} queued
          </strong>
          <p>
            Run <code>{result.batch_id}</code> is now the active batch. The dashboard will refresh as the ledger is written.
          </p>
          <div className="upload-progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} aria-label="Batch reconciliation progress">
            <span style={{ width: `${progress}%` }} />
          </div>
          <small className="upload-progress-label">{progress}% reconciled</small>
        </div>
        <button type="button" className="text-button" onClick={reset}>
          Upload another
        </button>
      </div>
    );
  }

  if (state.phase === "error") {
    return (
      <div className="uploader-result uploader-error" role="alert">
        <AlertCircle size={22} aria-hidden="true" />
        <div className="uploader-result-body">
          <strong>Upload failed</strong>
          <p>{state.message}</p>
        </div>
        <button type="button" className="text-button" onClick={reset}>
          Try again
        </button>
      </div>
    );
  }

  if (state.phase === "uploading") {
    return (
      <div className="uploader-zone uploader-uploading" aria-live="polite">
        <Loader2 size={24} className="spinner" aria-hidden="true" />
        <span>
          Processing <em>{state.filename}</em>&hellip;
        </span>
        <div className="upload-progress upload-progress-inline" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} aria-label="Batch reconciliation progress">
          <span style={{ width: `${progress}%` }} />
        </div>
      </div>
    );
  }

  return (
    <div
      className={`uploader-zone ${state.phase === "dragging" ? "uploader-dragging" : ""}`}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      role="button"
      tabIndex={0}
      aria-label="Upload settlement CSV file"
      onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
      onClick={() => inputRef.current?.click()}
    >
      <input
        ref={inputRef}
        type="file"
        accept=".csv,.json,text/csv,application/json"
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(e) => handleFiles(e.target.files)}
      />
      <Upload size={22} aria-hidden="true" />
      <div>
        <strong>Drop a settlement CSV or JSON array</strong>
        <small>
          <FileText size={13} aria-hidden="true" />
          &nbsp;or click to browse &mdash; max 10 MB
        </small>
      </div>
      {progressActive ? (
        <div className="upload-progress upload-progress-inline" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={progress} aria-label="Batch reconciliation progress">
          <span style={{ width: `${progress}%` }} />
        </div>
      ) : null}
    </div>
  );
}
