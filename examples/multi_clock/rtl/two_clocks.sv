// Multi-clock design: two independent clock domains
module two_clocks #(
    parameter WIDTH = 8
) (
    input  logic             clk_a,
    input  logic             clk_b,
    input  logic             rst_n,
    input  logic [WIDTH-1:0] a_data,
    input  logic             a_valid,
    output logic [WIDTH-1:0] b_data,
    output logic             b_valid
);

// 2-flop synchronizer on valid, data passed alongside (for demo only)
logic [1:0] sync_valid;
always_ff @(posedge clk_a or negedge rst_n) begin
    if (!rst_n) begin
        b_data  <= '0;
        b_valid <= 1'b0;
    end else begin
        b_data  <= a_data;
        b_valid <= a_valid;
    end
end

always_ff @(posedge clk_b or negedge rst_n) begin
    if (!rst_n) begin
        sync_valid <= 2'b00;
    end else begin
        sync_valid <= {sync_valid[0], b_valid};
    end
end

endmodule
