import axios from "axios";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import useSWR from "swr";
import { toast } from "sonner";

import { baseUrl } from "@/api/baseUrl";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

type TapoCamera = {
  name: string;
  display_name: string;
};

type TapoRecording = {
  camera_name: string;
  start_time: number;
  end_time: number;
  display_start_time: number;
  display_end_time: number;
};

type TapoRecordingsResponse = {
  recordings: TapoRecording[];
};

type TapoDownloadStartResponse = {
  job_id: string;
};

type TapoDownloadJob = {
  id: string;
  status: "running" | "completed" | "failed";
  completed: number;
  total: number;
  download_url?: string;
  error?: string;
};

type TapoApiError = {
  response?: {
    data?: {
      detail?: string;
    };
  };
};

type TapoSdCardDialogProps = {
  onClose: () => void;
};

function recordingKey(recording: TapoRecording) {
  return `${recording.camera_name}:${recording.start_time}:${recording.end_time}`;
}

function formatTimestamp(timestamp: number) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "short",
    timeStyle: "short",
  }).format(new Date(timestamp * 1000));
}

function yesterdayDate() {
  const date = new Date();
  date.setDate(date.getDate() - 1);
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

async function getApiErrorMessage(error: unknown): Promise<string | undefined> {
  const apiError = error as TapoApiError;
  const data = apiError.response?.data;
  if (data instanceof Blob) {
    try {
      const parsed = (await data.text()) as string;
      return (JSON.parse(parsed) as { detail?: string }).detail;
    } catch {
      return undefined;
    }
  }
  return data?.detail;
}

export default function TapoSdCardDialog({ onClose }: TapoSdCardDialogProps) {
  const { t } = useTranslation(["views/exports", "common"]);
  const { data: cameras, error: camerasError } =
    useSWR<TapoCamera[]>("tapo/cameras");
  const [selectedCameraNames, setSelectedCameraNames] = useState<string[]>([]);
  const [cameraSelectionInitialized, setCameraSelectionInitialized] =
    useState(false);
  const [recordingDate, setRecordingDate] = useState(yesterdayDate());
  const [username, setUsername] = useState("admin");
  const [cameraPassword, setCameraPassword] = useState("");
  const [cloudPassword, setCloudPassword] = useState("");
  const [recordings, setRecordings] = useState<TapoRecording[]>([]);
  const [selectedRecordingKeys, setSelectedRecordingKeys] = useState<
    Set<string>
  >(new Set());
  const [isListing, setIsListing] = useState(false);
  const [isDownloading, setIsDownloading] = useState(false);
  const [downloadJobId, setDownloadJobId] = useState<string>();
  const { data: downloadJob } = useSWR<TapoDownloadJob>(
    downloadJobId ? `tapo/downloads/${downloadJobId}` : null,
    {
      refreshInterval: (latestJob) =>
        latestJob?.status === "running" ? 2000 : 0,
    },
  );

  useEffect(() => {
    if (cameras && !cameraSelectionInitialized) {
      setSelectedCameraNames(cameras.map((camera) => camera.name));
      setCameraSelectionInitialized(true);
    }
  }, [cameraSelectionInitialized, cameras]);

  const selectedRecordings = useMemo(
    () =>
      recordings.filter((recording) =>
        selectedRecordingKeys.has(recordingKey(recording)),
      ),
    [recordings, selectedRecordingKeys],
  );

  const close = () => {
    setCameraPassword("");
    setCloudPassword("");
    onClose();
  };

  const toggleCamera = (cameraName: string) => {
    setSelectedCameraNames((current) =>
      current.includes(cameraName)
        ? current.filter((name) => name !== cameraName)
        : [...current, cameraName],
    );
  };

  const listRecordings = async () => {
    if (!recordingDate || selectedCameraNames.length === 0) {
      toast.error(t("tapo.validation.dateAndCamera"), {
        position: "top-center",
      });
      return;
    }

    setIsListing(true);
    setRecordings([]);
    setSelectedRecordingKeys(new Set());
    try {
      const response = await axios.post<TapoRecordingsResponse>(
        "tapo/recordings",
        {
          camera_names: selectedCameraNames,
          date: recordingDate,
          username,
          camera_password: cameraPassword,
          cloud_password: cloudPassword,
        },
      );
      setRecordings(response.data.recordings);
      setSelectedRecordingKeys(
        new Set(response.data.recordings.map(recordingKey)),
      );
    } catch (error) {
      const errorMessage = await getApiErrorMessage(error);
      toast.error(
        t("tapo.toast.listError", {
          errorMessage: errorMessage || t("tapo.toast.unknownError"),
        }),
        { position: "top-center" },
      );
    } finally {
      setIsListing(false);
    }
  };

  const downloadRecordings = async () => {
    if (selectedRecordings.length === 0) {
      return;
    }

    setIsDownloading(true);
    try {
      const response = await axios.post<TapoDownloadStartResponse>(
        "tapo/recordings/download",
        {
          date: recordingDate,
          recordings: selectedRecordings.map((recording) => ({
            camera_name: recording.camera_name,
            start_time: recording.start_time,
            end_time: recording.end_time,
          })),
          username,
          camera_password: cameraPassword,
          cloud_password: cloudPassword,
        },
      );
      setDownloadJobId(response.data.job_id);
      setCameraPassword("");
      setCloudPassword("");
      toast.success(t("tapo.toast.downloadStarted"), {
        position: "top-center",
      });
    } catch (error) {
      const errorMessage = await getApiErrorMessage(error);
      toast.error(
        t("tapo.toast.downloadError", {
          errorMessage: errorMessage || t("tapo.toast.unknownError"),
        }),
        { position: "top-center" },
      );
    } finally {
      setIsDownloading(false);
    }
  };

  const downloadArchive = () => {
    if (downloadJob?.download_url) {
      const url = `${baseUrl}api/${downloadJob.download_url}`;
      setDownloadJobId(undefined);
      window.location.assign(url);
    }
  };

  const groupedRecordings = useMemo(() => {
    const cameraNames = new Map(
      (cameras ?? []).map((camera) => [camera.name, camera.display_name]),
    );
    return recordings.map((recording) => ({
      ...recording,
      displayName:
        cameraNames.get(recording.camera_name) ?? recording.camera_name,
    }));
  }, [cameras, recordings]);

  return (
    <Dialog open={true} onOpenChange={(open) => !open && close()}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-2xl">
        <DialogTitle>{t("tapo.title")}</DialogTitle>
        <p className="text-sm text-muted-foreground">{t("tapo.description")}</p>

        <div className="space-y-2">
          <Label>{t("tapo.cameras")}</Label>
          {camerasError && (
            <p className="text-sm text-destructive">
              {t("tapo.cameraLoadError")}
            </p>
          )}
          {cameras?.length === 0 && (
            <p className="text-sm text-muted-foreground">
              {t("tapo.noCameras")}
            </p>
          )}
          {cameras?.map((camera) => (
            <label
              key={camera.name}
              className="flex cursor-pointer items-center gap-2 text-sm"
            >
              <input
                type="checkbox"
                className="size-4 accent-primary"
                checked={selectedCameraNames.includes(camera.name)}
                onChange={() => toggleCamera(camera.name)}
              />
              {camera.display_name}
            </label>
          ))}
        </div>

        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="tapo-recording-date">{t("tapo.date")}</Label>
            <Input
              id="tapo-recording-date"
              type="date"
              value={recordingDate}
              onChange={(event) => setRecordingDate(event.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tapo-camera-username">{t("tapo.username")}</Label>
            <Input
              id="tapo-camera-username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tapo-camera-password">
              {t("tapo.cameraPassword")}
            </Label>
            <Input
              id="tapo-camera-password"
              type="password"
              autoComplete="current-password"
              value={cameraPassword}
              onChange={(event) => setCameraPassword(event.target.value)}
            />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="tapo-cloud-password">
              {t("tapo.cloudPassword")}
            </Label>
            <Input
              id="tapo-cloud-password"
              type="password"
              autoComplete="off"
              value={cloudPassword}
              onChange={(event) => setCloudPassword(event.target.value)}
            />
          </div>
        </div>

        <p className="text-xs text-muted-foreground">
          {t("tapo.credentialsNote")}
        </p>
        <p className="text-xs text-muted-foreground">
          {t("tapo.storageCopyNote")}
        </p>

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={close}>
            {t("button.cancel", { ns: "common" })}
          </Button>
          <Button
            onClick={() => void listRecordings()}
            disabled={isListing || selectedCameraNames.length === 0}
          >
            {isListing ? t("tapo.listing") : t("tapo.findRecordings")}
          </Button>
        </div>

        {recordings.length === 0 && !isListing && (
          <p className="text-sm text-muted-foreground">
            {t("tapo.noRecordings")}
          </p>
        )}

        {recordings.length > 0 && (
          <div className="space-y-3 border-t pt-3">
            <div className="flex items-center justify-between gap-2">
              <p className="text-sm font-medium">
                {t("tapo.recordingCount", { count: recordings.length })}
              </p>
              <Button
                variant="secondary"
                size="sm"
                onClick={() =>
                  setSelectedRecordingKeys(
                    selectedRecordingKeys.size === recordings.length
                      ? new Set()
                      : new Set(recordings.map(recordingKey)),
                  )
                }
              >
                {selectedRecordingKeys.size === recordings.length
                  ? t("tapo.selectNone")
                  : t("tapo.selectAll")}
              </Button>
            </div>

            <div className="max-h-64 space-y-1 overflow-y-auto rounded-md border p-2">
              {groupedRecordings.map((recording) => (
                <label
                  key={recordingKey(recording)}
                  className="flex cursor-pointer items-center justify-between gap-3 rounded px-2 py-1.5 text-sm hover:bg-muted"
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <input
                      type="checkbox"
                      className="size-4 accent-primary"
                      checked={selectedRecordingKeys.has(
                        recordingKey(recording),
                      )}
                      onChange={() =>
                        setSelectedRecordingKeys((current) => {
                          const next = new Set(current);
                          const key = recordingKey(recording);
                          if (next.has(key)) {
                            next.delete(key);
                          } else {
                            next.add(key);
                          }
                          return next;
                        })
                      }
                    />
                    <span className="truncate">{recording.displayName}</span>
                  </span>
                  <span className="shrink-0 text-muted-foreground">
                    {formatTimestamp(recording.display_start_time)}
                    {" – "}
                    {formatTimestamp(recording.display_end_time)}
                  </span>
                </label>
              ))}
            </div>

            <div className="flex justify-end">
              <Button
                onClick={() => void downloadRecordings()}
                disabled={
                  isDownloading ||
                  downloadJob?.status === "running" ||
                  selectedRecordings.length === 0
                }
              >
                {isDownloading
                  ? t("tapo.startingDownload")
                  : t("tapo.downloadSelected", {
                      count: selectedRecordings.length,
                    })}
              </Button>
            </div>
          </div>
        )}

        {downloadJob?.status === "running" && (
          <div className="space-y-2 rounded-md border p-3">
            <p className="text-sm">
              {t("tapo.jobProgress", {
                completed: downloadJob.completed,
                total: downloadJob.total,
              })}
            </p>
            <progress
              className="h-2 w-full accent-primary"
              max={downloadJob.total}
              value={downloadJob.completed}
            />
          </div>
        )}

        {downloadJob?.status === "failed" && (
          <p className="text-sm text-destructive">
            {downloadJob.error || t("tapo.toast.unknownError")}
          </p>
        )}

        {downloadJob?.status === "completed" && (
          <div className="flex items-center justify-between gap-2 rounded-md border p-3">
            <p className="text-sm">{t("tapo.jobComplete")}</p>
            <Button
              onClick={downloadArchive}
              disabled={!downloadJob.download_url}
            >
              {t("tapo.downloadZip")}
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
