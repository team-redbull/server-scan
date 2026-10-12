import { fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { MultiSelect } from "@/features/inventory/MultiSelect";

const OPTIONS = [
  { value: "dell", label: "Dell", count: 3 },
  { value: "hp", label: "HP", count: 2 },
  { value: "cisco", label: "Cisco", count: 0 },
];

function Harness({ initial = [] as string[] }: { initial?: string[] }) {
  const [selected, setSelected] = useState(initial);
  return (
    <>
      <label htmlFor="v">Vendor</label>
      <MultiSelect
        id="v"
        label="Vendor"
        className=""
        allLabel="All (5)"
        options={OPTIONS}
        selected={selected}
        onToggle={(value) =>
          setSelected((cur) =>
            cur.includes(value)
              ? cur.filter((v) => v !== value)
              : [...cur, value],
          )
        }
        onClear={() => {
          setSelected([]);
        }}
      />
      <p>outside</p>
    </>
  );
}

const button = () => screen.getByLabelText("Vendor");
const box = (name: RegExp) =>
  within(screen.getByRole("group", { name: "Vendor options" })).getByRole(
    "checkbox",
    { name },
  );

describe("MultiSelect", () => {
  it("reads All while nothing is ticked, so none means every value", () => {
    render(<Harness />);

    expect(button()).toHaveTextContent("All (5)");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("takes one value, two, or more, and summarises them", () => {
    render(<Harness />);
    fireEvent.click(button());

    fireEvent.click(box(/^Dell/));
    expect(button()).toHaveTextContent("Dell");
    fireEvent.click(box(/^HP/));
    expect(button()).toHaveTextContent("Dell, HP");
    fireEvent.click(box(/^Cisco/));
    expect(button()).toHaveTextContent("3 selected");
    fireEvent.click(box(/^Dell/));
    expect(button()).toHaveTextContent("HP, Cisco");
  });

  it("shows each option's count next to it", () => {
    render(<Harness />);
    fireEvent.click(button());

    expect(box(/^Dell 3$/)).toBeInTheDocument();
    expect(box(/^Cisco 0$/)).toBeInTheDocument();
  });

  it("clears back to all from the list", () => {
    render(<Harness initial={["dell", "hp"]} />);
    fireEvent.click(button());

    fireEvent.click(screen.getByRole("button", { name: /Clear/ }));

    expect(button()).toHaveTextContent("All (5)");
  });

  it("closes on Escape and on a click outside", () => {
    render(<Harness />);
    fireEvent.click(button());
    expect(screen.getByRole("group")).toBeInTheDocument();

    fireEvent.keyDown(screen.getByRole("group"), { key: "Escape" });
    expect(screen.queryByRole("group")).not.toBeInTheDocument();

    fireEvent.click(button());
    fireEvent.mouseDown(screen.getByText("outside"));
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });
});
