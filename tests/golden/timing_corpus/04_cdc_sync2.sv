// Golden: 2-flop synchronizer across clock domains.
module cdc_sync2 (
    input  clk_a,
    input  clk_b,
    input  d_in,
    output d_out
);
reg sync1, sync2;
always_ff @(posedge clk_a) sync1 <= d_in;
always_ff @(posedge clk_b) sync2 <= sync1;
assign d_out = sync2;
endmodule
