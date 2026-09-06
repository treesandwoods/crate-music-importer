import { Action, ActionPanel, Clipboard, Form, Icon } from "@raycast/api";
import { useEffect, useState } from "react";

import { SourcePreviewView } from "./source-preview";

export default function Command() {
  const [url, setUrl] = useState("");
  const [error, setError] = useState<string>();

  useEffect(() => {
    void Clipboard.readText().then((value) => {
      if (value?.includes("open.spotify.com/playlist/")) setUrl(value.trim());
    });
  }, []);

  const valid = /^https?:\/\/(open|play)\.spotify\.com\/playlist\/[A-Za-z0-9]{16,32}/.test(url.trim());
  return (
    <Form
      navigationTitle="Playlist Link Import"
      actions={
        <ActionPanel>
          {valid ? (
            <Action.Push
              title="Preview Playlist"
              icon={Icon.List}
              target={<SourcePreviewView type="playlist" url={url.trim()} />}
            />
          ) : null}
        </ActionPanel>
      }
    >
      <Form.TextField
        id="url"
        title="Spotify Playlist"
        placeholder="https://open.spotify.com/playlist/…"
        value={url}
        onChange={(value) => {
          setUrl(value);
          setError(
            value && !/^https?:\/\/(open|play)\.spotify\.com\/playlist\//.test(value)
              ? "Use a public Spotify playlist link."
              : undefined,
          );
        }}
        error={error}
        autoFocus
      />
      <Form.Description text="Preview loads Spotify metadata and checks the local Music cache. YouTube matching and downloading begin only after a second confirmation." />
    </Form>
  );
}
