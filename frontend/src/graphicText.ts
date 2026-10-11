export type GraphicAction = "generate" | "revise";

/** Backend wording for the version that first adds a graphic. */
const GENERATED_REQUEST = "Generated campaign graphic.";

export function graphicChange(item: {
  revision_request: string | null;
  revision_targets: string[];
}): "added" | "updated" | null {
  if (!item.revision_targets.includes("primary_graphic")) return null;
  return item.revision_request === GENERATED_REQUEST ? "added" : "updated";
}

export function graphicChangeText(
  change: "added" | "updated",
  versionNumber: number,
) {
  return `Graphic ${change} in Version ${versionNumber}.`;
}

export function graphicErrorMessage(code: string): string | null {
  const messages: Record<string, string> = {
    EVENT_CORRECTION_REQUIRED:
      "That changes the event details, not just the graphic. Update the Event first, then create a fresh campaign version and graphic.",
    GRAPHIC_GENERATION_FAILED:
      "I couldn’t finish the graphic. Your current campaign version and its approval are unchanged, so you can try again.",
    GRAPHIC_OUTPUT_INVALID:
      "I couldn’t finish the graphic safely. Your current campaign version is unchanged, so you can try again.",
  };
  return messages[code] ?? null;
}
