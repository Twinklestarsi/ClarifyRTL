// Generated deterministically from Hardware-aware Petri Net IR.
// Do not edit generated RTL; edit the IR/specification instead.
module counter_a (
    input clk,
    input reset,
    input clear,
    input load,
    input enable,
    input [3:0] data_in,
    output reg [3:0] count
);

reg [3:0] __pn_next_count;

always @* begin
    __pn_next_count = count;
    if (load) begin
        __pn_next_count = data_in;
    end
    else if (clear) begin
        __pn_next_count = 4'd0;
    end
    else if (enable) begin
        __pn_next_count = count + 4'd1;
    end
end

// Sequential process: update_count
always @(posedge clk) begin
    if (reset) begin
        count <= 4'd0;
    end else begin
        count <= __pn_next_count;
    end
end

endmodule
