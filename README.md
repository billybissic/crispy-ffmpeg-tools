# Running the Media Processing Pipeline

This project provides a distributed FFmpeg processing pipeline backed by a local SQLite queue on each worker node.

Each node operates independently:

* its own `media-processing.db`
* its own processing directory
* its own FFmpeg process
* its own queue state

Nodes coordinate through a shared claim directory located on the shared media filesystem. The claim system prevents multiple workers from processing the same source file at the same time.

---

## Pipeline Overview

Normal file lifecycle:

```text
READY_TO_MOVE
    ↓
IN_PROCESSING
    ↓
READY_TO_RETURN
    ↓
RETURNED
```

Other terminal states:

```text
FAILED
SKIPPED
```

`SKIPPED` is normally used when another processing node has already claimed a source file.

A typical processing cycle is:

```text
scan media library
    ↓
queue eligible files
    ↓
claim source
    ↓
move source into node processing directory
    ↓
encode with FFmpeg
    ↓
verify output duration
    ↓
verify output is smaller
    ↓
adopt rendered output
    ↓
return rendered file to original directory
```

---

# Requirements

The worker must have:

```text
Python 3.10+
FFmpeg
ffprobe
SQLite
Git
```

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install -y ffmpeg sqlite3 python3 python3-venv git
```

Verify FFmpeg:

```bash
ffmpeg -version | head -n 1
ffprobe -version | head -n 1
```

---

# Clone the Repository

```bash
git clone git@github.com:billybissic/crispy-ffmpeg-tools.git
cd crispy-ffmpeg-tools
```

---

# Python Installation

## Ubuntu 22.04 / User Installation

If the system Python allows user installs:

```bash
python3 -m pip install .
```

After future Git updates:

```bash
git pull --ff-only
python3 -m pip install --no-cache-dir .
```

---

## Ubuntu 24.04 / Virtual Environment

Ubuntu 24.04 may prevent installing packages directly into the system Python environment.

Create a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install the project:

```bash
python -m pip install .
```

For future sessions:

```bash
cd ~/projects/crispy-ffmpeg-tools
source .venv/bin/activate
```

After updates:

```bash
git pull --ff-only
source .venv/bin/activate
python -m pip install .
```

---

# Configure the Shared Claims Directory

All worker nodes must use a claim directory that resolves to the same physical shared filesystem.

Example physical layout:

```text
media/
└── processing/
    ├── .claims/
    ├── mini-dev/
    ├── mini-nas/
    └── mini-nas-two/
```

Create the claim directory:

```bash
mkdir -p /path/to/shared/media/processing/.claims
```

Set:

```bash
export MEDIA_QUEUE_CLAIMS_DIR=/path/to/shared/media/processing/.claims
```

To make it persistent:

```bash
echo 'export MEDIA_QUEUE_CLAIMS_DIR=/path/to/shared/media/processing/.claims' >> ~/.bashrc
source ~/.bashrc
```

Verify:

```bash
echo "$MEDIA_QUEUE_CLAIMS_DIR"
```

### Example Node Paths

Different workers may mount the same physical directory at different paths.

Example:

```text
mini-dev
/mnt/rdisk/processing/.claims

mini-nas
/mnt/rdisk/media/processing/.claims

mini-nas-two
/mnt/rdisk-remote/media/processing/.claims
```

These paths must all resolve to the same physical `.claims` directory.

---

# Create the Node Processing Directory

Each worker must have its own isolated processing directory.

Example:

```bash
mkdir -p /mnt/rdisk/media/processing/mini-nas
```

or:

```bash
mkdir -p /mnt/rdisk-remote/media/processing/mini-nas-two
```

Do not allow multiple nodes to use the same processing directory.

---

# Initialize the Queue Database

Each node maintains its own local SQLite database.

From the project directory:

```bash
media-queue --db ./media-processing.db init
```

This also performs supported database schema upgrades.

---

# Scan the Media Library

Example:

```bash
media-queue --db ./media-processing.db scan \
  "/mnt/rdisk/media/Movies" \
  --processing-dir /mnt/rdisk/media/processing/mini-nas \
  --min-size-gb 10 \
  --pipeline shrink-h264-v1
```

Example output:

```text
Queued/updated: 94; already processed: 0; skipped/errors: 0
```

The scan only populates the local node queue.

It does not assign ownership of a file.

Ownership happens when `move-to-processing` successfully acquires the shared claim.

---

# Inspect the Queue

Ready files:

```bash
media-queue --db ./media-processing.db list --status READY_TO_MOVE
```

Currently staged:

```bash
media-queue --db ./media-processing.db list --status IN_PROCESSING
```

Completed:

```bash
media-queue --db ./media-processing.db list --status RETURNED
```

Skipped because another node claimed them:

```bash
media-queue --db ./media-processing.db list --status SKIPPED
```

Failed:

```bash
media-queue --db ./media-processing.db list --status FAILED
```

---

# Stage Files for Processing

A worker should attempt queue candidates until the desired number of files are actually staged.

Do not simply select 20 IDs and assume all 20 will be available.

Another node may already own some of them.

Example: fill the worker to 20 staged files.

```bash
target=20

count=$(media-queue --db ./media-processing.db list --status IN_PROCESSING \
  | awk 'NR>1 {count++} END {print count+0}')

echo "Already staged: $count/$target"

while IFS= read -r id; do
  [[ "$count" -ge "$target" ]] && break

  media-queue --db ./media-processing.db move-to-processing "$id" || true

  status=$(sqlite3 ./media-processing.db \
    "SELECT status FROM processing_queue WHERE id=$id;")

  if [[ "$status" == "IN_PROCESSING" ]]; then
    ((count++))
    echo "STAGED $id ($count/$target)"
  fi

done < <(
  media-queue --db ./media-processing.db list --status READY_TO_MOVE \
    | awk 'NR>1 {print $1}'
)
```

Claim collisions are expected in a distributed environment.

Example:

```text
SKIPPED: item 38 is already claimed by another processing node
```

The local queue item is marked:

```text
SKIPPED
```

and the worker continues without touching the source.

---

# Start the Processing Batch

Run all currently staged files:

```bash
./process-staged-batch.sh \
  --on-duration-mismatch skip \
  $(media-queue --db ./media-processing.db list --status IN_PROCESSING \
    | awk 'NR>1 {print $1}')
```

The worker processes files sequentially.

A larger staged queue therefore increases unattended runtime without increasing the number of simultaneous FFmpeg encodes on that worker.

---

# Duration Mismatch Behavior

The batch processor supports:

```text
--on-duration-mismatch stop
--on-duration-mismatch skip
--on-duration-mismatch delete
```

Recommended distributed-worker behavior:

```bash
--on-duration-mismatch skip
```

With `skip`:

* the queue item becomes `FAILED`
* the source and rejected render are preserved
* the failure is displayed in the terminal
* later IDs continue processing

Example:

```text
FAIL 136: Duration mismatch
files preserved for inspection
SKIP 136
```

---

# Shared Claim Behavior

Before moving a source file into a processing directory, the worker creates an atomic shared claim based on the source fingerprint.

Conceptually:

```text
READY_TO_MOVE
      ↓
calculate source fingerprint
      ↓
attempt shared claim
      ↓
┌───────────────┬─────────────────────┐
│ claim success │ claim already exists│
└───────┬───────┴──────────┬──────────┘
        ↓                  ↓
IN_PROCESSING          SKIPPED
```

Claims intentionally remain after successful processing.

This prevents an old or stale queue database on another worker from later processing the same original source.

Local queue IDs are node-specific and are not used as cluster-wide identifiers.

The shared source fingerprint is the distributed identity.

---

# Updating a Worker Node

Do not update a node while it is actively processing a batch.

Allow the active batch to finish first.

Then:

```bash
cd ~/projects/crispy-ffmpeg-tools
git pull --ff-only
```

For a normal Python installation:

```bash
python3 -m pip install --no-cache-dir .
```

For a virtual environment:

```bash
source .venv/bin/activate
python -m pip install .
```

Upgrade/validate the database:

```bash
media-queue --db ./media-processing.db init
```

Verify claims:

```bash
echo "$MEDIA_QUEUE_CLAIMS_DIR"
```

Verify CLI:

```bash
media-queue --help
```

Run tests when appropriate:

```bash
python3 -m pytest
```

---

# Useful Queue Commands

Show one queue item:

```bash
media-queue --db ./media-processing.db show 42
```

Mark a known bad source as failed:

```bash
media-queue --db ./media-processing.db mark-failed 42 \
  --reason "Source file corrupt"
```

Reset an inspected failed item:

```bash
media-queue --db ./media-processing.db reset-failed 42
```

List processed fingerprints:

```bash
media-queue --db ./media-processing.db footprints
```

---

# Operational Safety

The pipeline intentionally favors safety over silently continuing.

Important safeguards include:

* no silent destination overwrite
* source fingerprint validation
* persistent completed-file fingerprints
* shared distributed claims
* duration verification
* output-size verification
* isolated processing directories
* local queue audit history
* explicit `FAILED` state
* explicit `SKIPPED` state

If a move, encode, verification, or return operation behaves unexpectedly, inspect the queue record before manually moving or deleting files.

---

# Current Distributed Architecture

```text
Shared Media RAID
│
├── Movies/
│
└── processing/
    ├── .claims/
    ├── mini-dev/
    ├── mini-nas/
    └── mini-nas-two/

mini-dev
├── local media-processing.db
├── media-queue
└── FFmpeg worker

mini-nas
├── local media-processing.db
├── media-queue
└── FFmpeg worker

mini-nas-two
├── local media-processing.db
├── media-queue
└── FFmpeg worker
```

The SQLite databases remain independent.

The shared `.claims` directory coordinates distributed file ownership.
