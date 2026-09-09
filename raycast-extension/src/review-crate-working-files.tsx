import { Action, ActionPanel, Alert, confirmAlert, Detail, Icon, List } from "@raycast/api";
import { useEffect, useState } from "react";
import { workingFiles, WorkingFiles } from "./backend";

export default function Command() {
  const [result, setResult] = useState<WorkingFiles>();
  const [error, setError] = useState<string>();
  async function refresh() {
    try {
      setResult(await workingFiles());
      setError(undefined);
    } catch (caught) {
      setError(String(caught));
    }
  }
  async function cleanup(row: WorkingFiles["files"][number]) {
    if (
      !(await confirmAlert({
        title: "Move this working file to Trash?",
        message: `${row.path} (${row.size_bytes} bytes). ${row.reason}`,
        primaryAction: { title: "Move to Trash", style: Alert.ActionStyle.Destructive },
      }))
    )
      return;
    try {
      setResult(await workingFiles(row));
    } catch (caught) {
      setError(String(caught));
    }
  }
  useEffect(() => {
    void refresh();
  }, []);
  if (error) return <Detail markdown={`# Working files review failed\n\n${error}`} />;
  return (
    <List isLoading={!result} navigationTitle="Review Crate Working Files">
      <List.Section
        title={`${result?.files.length || 0} files · ${((result?.total_bytes || 0) / 1024 ** 3).toFixed(2)} GiB`}
      >
        {result?.files.map((row) => (
          <List.Item
            key={row.path}
            title={row.path.split("/").pop() || row.path}
            subtitle={row.category}
            accessories={[{ text: `${(row.size_bytes / 1024 ** 2).toFixed(1)} MiB` }]}
            actions={
              <ActionPanel>
                <Action.Push
                  title="Review Evidence"
                  icon={Icon.Document}
                  target={
                    <Detail
                      markdown={`# ${row.category}\n\n${row.reason}\n\n${row.path}\n\nNothing has been removed.`}
                    />
                  }
                />
                {row.cleanup_eligible && (
                  <Action title="Move Reviewed File to Trash" icon={Icon.Trash} onAction={() => cleanup(row)} />
                )}
                <Action.ShowInFinder path={row.path} />
                <Action title="Refresh Review" icon={Icon.ArrowClockwise} onAction={refresh} />
              </ActionPanel>
            }
          />
        ))}
      </List.Section>
    </List>
  );
}
