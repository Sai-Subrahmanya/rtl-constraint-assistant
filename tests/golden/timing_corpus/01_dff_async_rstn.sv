// Golden: single dff with async active-low reset.
module dff_async_rstn (
    input  clk,
    input  rst_n,
    input  d,
    output reg q
);
always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) q <= 1'b0;
    else        q <= d;
endmodule
