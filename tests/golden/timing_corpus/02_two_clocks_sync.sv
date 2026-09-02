// Golden: two registers on unrelated clocks; user tells us they're synchronous.
module two_clocks_sync (
    input  clk_a,
    input  clk_b,
    input  d,
    output reg q1,
    output reg q2
);
always_ff @(posedge clk_a) q1 <= d;
always_ff @(posedge clk_b) q2 <= d;
endmodule
