module counter(
  input logic clk,
  input logic rst_n,
  input logic en,
  output logic [7:0] q
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) q <= '0;
    else if (en) q <= q + 1'b1;
  end
endmodule
