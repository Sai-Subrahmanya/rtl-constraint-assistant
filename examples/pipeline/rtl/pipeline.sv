// Two-stage pipeline example for RCA
module pipeline #(
    parameter WIDTH = 8
) (
    input  logic             clk,
    input  logic             rst_n,
    input  logic [WIDTH-1:0] in_data,
    input  logic             in_valid,
    output logic [WIDTH-1:0] out_data,
    output logic             out_valid
);

logic [WIDTH-1:0] stage1_data;
logic             stage1_valid;

always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
        stage1_data  <= '0;
        stage1_valid <= 1'b0;
        out_data     <= '0;
        out_valid    <= 1'b0;
    end else begin
        stage1_data  <= in_data + 1'b1;
        stage1_valid <= in_valid;
        out_data     <= stage1_data;
        out_valid    <= stage1_valid;
    end
end

endmodule
