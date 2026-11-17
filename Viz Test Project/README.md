# Viz Test

`Viz Test.app` is built from the copied Python files in `Source/`. It has a separate bundle identifier, window title, annotation database, and cache from `00 Viz.app`.

## Open it

Double-click `../Viz Test.app`. Keep `../Viz Test Support` beside the app. On this Mac, the test library points to `/Users/apple/Desktop/Others/UCSF/raw_UCSF`. A collaborator should set their own recording path as described under **Build from a clone**. The launcher rejects write-mode Python file opens for MAP, MPX, and PLX recordings. Its annotation database and cache live in `../Viz Test Support`, so changes made in the test app do not affect the original app's settings.

Viz Test 3.5.3 includes Viz_v2's PLX reader, MAP/MPX/PLX filters, per-channel plot height handles, updated patient metadata, and saved PLX sorts and flags. The imported PLX sorts remain subject to the test app's single-unit review and crop rules. Viz_v2's database was read without modifying it, and existing Viz Test MAP/MPX annotations were preserved. The annotation backup is `../Viz Test Support/Workspace/00 Viz/Data/recording_flags.before-v2-20261111-144341.db`.

Double-click (or double-tap on a trackpad) a non-electrode trace to open it in a larger inspection window. The new window starts with the same visible time range and voltage scale, supports plot pan and zoom, and has Restore view and Full recording buttons. Electrode traces retain their Sort spikes selection behavior.

In the main traces and enlarged recording window, hold **Shift** while scrolling over a plot to zoom only in recorded time. In **Spectra**, the same gesture zooms only in frequency. The vertical voltage or power range stays fixed.

In **Spectra**, tick **60 Hz notch** to calculate the displayed spectra after filtering each continuous recording run at 60 Hz. It filters the data used for the spectrum; it does not modify the recording files or change the channel quality labels. Untick it to see the original spectra again.

Click a channel in the Spectra list to highlight its plotted curve while keeping the current zoom. You can also click a curve to highlight its channel in the list; the name appears above the plot. The count and names of checked channels appear in a scrollable area below the plot.

For a PLX recording, click **Label Channels** in Spectra. Assign Spike, ECoG, EMG, or Other to each channel and click **Save**. Each choice is saved for the patient and applies to the same named channel in all that patient's PLX neuron recordings. Channels absent from the current recording retain their saved labels. Earlier per-recording labels are carried forward automatically. Crop Data uses the patient-wide labels to list spike, ECoG, and EMG channels; spike channels without a compatible saved sort appear as **sort needed**. The **Field inputs** button in Spectra uses channels labeled ECoG. The exported `good_channels.json` includes the matching saved choices in every recording's `channel_labels` list, with that recording's own channel IDs. Relabeling a cropped spike channel as something else makes that crop inactive without erasing its saved times.

Each neuron recording has a **Neuron notes** text box below its traces. Click **Save note** to store the text. Unsaved edits are also saved when you change recordings or close Viz Test. Notes are kept in `../Viz Test Support/Workspace/00 Viz/Data/recording_flags.db`, keyed to the recording, and load again when you reopen it. Saving a note does not change the raw recording files.

## Crop Data review

Open a participant and click **Crop Data**. The pop-up lists all of that participant's recordings, including formats hidden by the main-window filters. For each recording:

1. Choose a channel with a compatible saved spike sort and click **Crop to first 1,000 spikes in selected cluster**. Viz Test uses the cluster you selected when saving that sort. It records the first and thousandth spike times from that cluster, opens that recording in the main window, and immediately zooms the main trace and EMG preview to that interval. The main and enlarged time-series views stay within the crop. Spectra remains based on the whole recording. If the selected cluster has fewer than 1,000 spikes, the recording remains uncropped and the export flags it.
2. Check each good ECoG channel. These choices apply to the whole patient and appear on every recording of that patient; the export lists the matching channels for each recording.
3. Select an EMG channel and click **Add tremor segment**. Drag the shaded segment or its edges to adjust it. Add or remove segments as needed. These annotations are saved per EMG channel and per recording. The EMG menu shows how many segments are saved for each channel. Reopening Crop Data selects a channel with saved segments and shows its saved times immediately, even while the waveform loads.

Changes save to Viz Test's separate annotation database. The original recordings stay read-only. Existing per-recording ECoG choices from earlier test builds are combined into patient-level choices when first opened. Earlier crops based on all detected spikes are recalculated from the saved selected cluster when the recording is opened or exported. Click **Export good_channels.json** in the pop-up to choose a destination. The JSON includes every discovered recording path, crop status and times, total detected spike count, selected cluster number (as shown in Sort spikes), selected-cluster spike count, selected spike channel, patient-level good ECoG choices matched to that recording, and EMG tremor segments. For `.mpx` recordings, `spike_channels` lists all recorded SPK channels. For `.plx` recordings, it lists recorded high-rate neural channels; saved sorted channels remain listed if a file is temporarily unavailable. If no crop channel has been selected, `spike_channel` uses the first listed channel. The `status` still reports whether the recording has been reviewed and cropped. Exported tremor intervals are clipped to the saved crop. An older spike sort that no longer matches its source file must be repeated before cropping.

## Single-unit review

Open **Sort spikes** for a spike channel and choose the **Single-unit review · 3 ms ISI** tab. A previously saved sort opens directly on this tab. For the selected cluster, it shows an interspike-interval histogram with the 0–3 ms region shaded and its short-interval count, plus spike amplitude across recorded time. If the recording has an ECG or pulse channel, its trace appears on the same time axis. The tab scrolls on smaller displays while the cluster and save buttons remain visible. These views support the Shimamoto et al. single-unit checks; the reviewer still decides whether the refractory period is clear and the waveforms are distinct. Do not increase the peak detector spacing to hide short intervals.

Record a decision of **Needs review**, **Accepted**, or **Rejected**. Acceptance requires at least 1,000 spikes in the selected cluster, confirmation of distinct waveforms and a clear 3 ms refractory period, and a documented cardiac-cycle assessment showing no linked waveform change. If there is no ECG or pulse channel in Viz Test, use an external cardiac reference or mark the assessment as unavailable. Click **Save unit review** for an already saved sort, or **Apply** to save a new sort and its review together. The review is kept in the test annotation database and becomes stale automatically if the saved sort or selected cluster changes.

Crop Data displays the single-unit review state. New exports include `single_unit_review` and `single_unit_analysis_ready`; the latter is true only for an accepted saved cluster with a current 1,000-spike crop. This flag covers the paper's single-unit acceptance step only; STN location and movement-response eligibility still require separate review. Existing exports must be generated again to include these fields.

## Build from a clone

This project is intended to be the repository root. Clone it into a folder named `Viz Test Project` (or any other name), then on an Apple silicon Mac install Python 3.12 and run from Terminal:

```sh
cd "/path/to/Viz Test Project"
./rebuild-viz-test.sh
```

The script creates `.venv` on its first run, installs the pinned Python packages, and builds `Viz Test.app` **beside** the project folder. It signs the app locally with an ad hoc signature. It targets Apple silicon (`arm64`); other Macs need a change to the build script. Internet access is needed for the first dependency install. Close Viz Test before rebuilding because the script replaces the built app. It preserves the neighboring `Viz Test Support` folder.

The recordings and annotations are **not** in Git. Obtain the MAP, MPX, and PLX files separately and keep them in their `raw_UCSF` patient-folder layout. Before the first launch, point your local test support folder at that library (replace the example path with your own absolute path):

```sh
mkdir -p "../Viz Test Support/Workspace/00 Viz"
printf '%s\n' '/absolute/path/to/raw_UCSF' > "../Viz Test Support/Workspace/00 Viz/library-location.txt"
```

Run those commands from the `Viz Test Project` folder. Keep `Viz Test Support` beside the app and project. The app creates its own annotation database and cache there; these are local to each collaborator. Existing sorts, notes, crop decisions, channel labels, and other annotations do not automatically travel through Git. Share them separately only if intended and permitted for the recordings.

## Pull and rebuild after source changes

With Viz Test closed, pull the latest changes in `Viz Test Project` and run `./rebuild-viz-test.sh` from that folder. Commit changes to `Source/`, `requirements-build.txt`, `rebuild-viz-test.sh`, and this README. The project `.gitignore` excludes the virtual environment, build products, backup app/source copies, caches, and database files. Do not add `raw_UCSF`, `Viz Test Support`, or exported patient data to the repository.

The original `00 Viz` source, app, and raw recordings are not changed during rebuilding. The older test source and app backups on this Mac remain local and are excluded from Git.

## Initial verification

The packaged 3.5.3 app found 94 patient folders in `raw_UCSF` and opened representative MAP, MPX, and PLX recordings without reader errors. It displayed the real `UCSF_193_STN_neuron_1.plx` waveform and loaded its imported sort with 1,605 selected-cluster spikes. The first 1,000 define a crop from 0.0565 to 43.8593 seconds. Source GUI checks covered the PLX filter, plot height resizing, notes, enlarged non-electrode trace, Spectra selection, and Shift-scroll zoom in all three windows. The channel-label UI check opened 11 dropdowns for a real PLX recording, saved Spike/ECoG/EMG choices in an isolated support folder, and confirmed that Crop Data listed those roles. Export and crop-invalidation checks passed. Code signing and SQLite integrity checks passed. Every older Viz Test row in flags, sorts, notes, crops, ECoG choices, and single-unit reviews remained unchanged. The pre-label annotation backup is `../Viz Test Support/Workspace/00 Viz/Data/recording_flags.before-channel-labels-20261117.db`. Viz Test discovers 531 recordings: 126 MAP, 74 MPX, and 331 PLX. Files under `excluded` are intentionally skipped.
