// Golden: synchronous reset (not in sensitivity list).
module sync_reset (
    input  clk,
    input  rst,
    input  d,
    output reg q
);
always_ff @(posedge clk)
    if (rst) q <= 1'b0;
    else     q <= d;
endmodule
