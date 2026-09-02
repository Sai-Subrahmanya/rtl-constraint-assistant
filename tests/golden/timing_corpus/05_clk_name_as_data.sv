// Golden: signal named "clk_sel" is data; not a clock.
module clk_name_as_data (
    input  clk,
    input  clk_sel,
    input  b,
    output reg y
);
always_ff @(posedge clk)
    if (clk_sel) y <= b;
    else         y <= 1'b0;
endmodule
