module m(input clk, d, output reg q);
  always_ff @(posedge clk) q <= d;
endmodule
