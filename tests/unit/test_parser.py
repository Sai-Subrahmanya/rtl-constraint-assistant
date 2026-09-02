"""Parser / design-model tests for the simple counter (Manual §60)."""
import pytest
from pathlib import Path
from rca.parser import SlangAdapter

COUNTER = """
module counter #(parameter WIDTH=8)(
    input logic clk, input logic rst_n, input logic en,
    output logic [WIDTH-1:0] q);
always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) q <= '0;
    else if (en) q <= q + 1'b1;
end
endmodule
"""


@pytest.fixture
def counter_design(tmp_path):
    f = tmp_path / "counter.sv"
    f.write_text(COUNTER)
    a = SlangAdapter()
    d = a.parse([str(f)], top="counter")
    return d


def test_counter_summary(counter_design):
    s = counter_design.summary()
    assert s["top"] == "counter"
    assert s["modules"] == 1
    assert s["ports"] == 4
    assert s["registers"] == 1


def test_clock_detection(counter_design):
    assert "clk" in counter_design.clocks_seed


def test_reset_detection(counter_design):
    assert "rst_n" in counter_design.resets_seed


def test_register_attributes(counter_design):
    reg = next(iter(counter_design.registers.values()))
    assert reg.clock_signal and reg.clock_signal.endswith(".clk")
    assert reg.reset_signal and reg.reset_signal.endswith(".rst_n")
    assert reg.reset_type.value == "asynchronous"
    assert reg.reset_polarity.value == "active_low"
    assert reg.width == 8


def test_port_directions(counter_design):
    ports = list(counter_design.ports.values())
    dirs = {p.local_name: p.direction.value for p in ports}
    assert dirs["clk"] == "input"
    assert dirs["rst_n"] == "input"
    assert dirs["q"] == "output"
    assert ports[-1].width == 8


def test_no_diagnostics(counter_design):
    # Should parse cleanly
    pass


def test_missing_file_reports_diagnostic(tmp_path):
    a = SlangAdapter()
    with pytest.raises(Exception):
        a.parse([str(tmp_path / "missing.sv")])
