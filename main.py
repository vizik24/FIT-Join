import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from merge_fit import merge_fit_files

app = FastAPI(title="FITJoin")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/merge")
def merge(files: list[UploadFile] = File(...)):
    # Only accept .fit files.
    for upload in files:
        if not (upload.filename or "").lower().endswith(".fit"):
            raise HTTPException(status_code=400, detail=f"{upload.filename} is not a .fit file.")

    # Save the uploads to a temporary folder so the merge code can read them.
    work_dir = Path(tempfile.mkdtemp())
    try:
        input_paths = []
        for i, upload in enumerate(files):
            path = work_dir / f"input_{i}.fit"
            path.write_bytes(upload.file.read())
            input_paths.append(path)

        output_path = work_dir / "merged_activity.fit"
        merge_fit_files(input_paths, output_path)
    except ValueError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise

    # Send the merged file back, then delete the temporary folder.
    return FileResponse(
        output_path,
        media_type="application/octet-stream",
        filename="merged_activity.fit",
        background=BackgroundTask(shutil.rmtree, work_dir, ignore_errors=True),
    )